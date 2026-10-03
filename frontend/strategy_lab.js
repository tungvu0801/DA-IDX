// strategy_lab.js — Stage 3.1 STRATEGY LAB: build, validate, save and version explicit strategy rules.
//
// Definitions only: nothing here backtests, paper trades, creates orders or calls Claude. The rules are data —
// registered features, whitelisted operators and literal values — built with menus (no JSON editing, no code).
// Every check (validation, readiness, summary, hashes) comes from the server's deterministic strategy module.
// Opening the tab loads the feature registry and the saved strategies only (local, no market / broker / event I/O);
// "Use current watchlist" / "Use my holdings" are explicit, read-only copies into the builder.
(function () {
  const root = document.getElementById("sl-body");
  if (!root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const getJSON = (u) => fetch(u).then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const send = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
    .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const READY = { BACKTEST_READY: ["BACKTEST READY", "ok"], FORWARD_TEST_ONLY: ["FORWARD TEST ONLY", "warn"], UNSUPPORTED: ["UNSUPPORTED", "dim"] };
  const readyTag = (s) => tag((READY[s] || [s])[0], (READY[s] || [0, "dim"])[1]);
  const STATE = { HISTORICAL: ["✓", "ok", "historical"], FORWARD_ONLY: ["⚠", "warn", "forward only"], UNSUPPORTED: ["✕", "dim", "not usable yet"] };
  const OP_LABEL = { ">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", "==": "is", "!=": "is not",
    in: "is one of", not_in: "is none of", between: "is between", is_true: "is yes", is_false: "is no" };
  const when = (v) => (v ? new Date(v).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");

  let reg = null, byId = {}, list = [], showArchived = false, advanced = false;
  let draft = null, mode = "new", editing = null, viewing = null, viewVersion = null, report = null, seq = 0, notice = "";
  let compareSel = [], compareOut = null;

  const blank = () => ({ schema_version: reg.schema_version, name: "", description: "", direction: "LONG_ONLY", timeframe: "1D",
    execution: { ...reg.execution }, universe: { type: "EXPLICIT_SYMBOLS", symbols: [], origin: "MANUAL" },
    entry: { logic: "ALL", conditions: [] }, exit: { logic: "ANY", conditions: [], invalidation: null, target: null, max_holding_days: null },
    risk: { max_position_pct: 10, max_open_positions: 5 } });
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const editable = () => mode === "new" || mode === "edit" || mode === "example";

  // ---- condition rows ----------------------------------------------------------------------------------------
  function featureOptions(sel) {
    const groups = {};
    reg.features.forEach((f) => { (groups[f.scope] = groups[f.scope] || []).push(f); });
    return Object.entries(groups).map(([scope, fs]) => `<optgroup label="${esc(scope)}">${fs.map((f) => {
      const mark = f.historical_support ? "" : f.forward_support ? " ⚠ forward only" : " ✕ not usable yet";
      return `<option value="${esc(f.feature_id)}"${f.feature_id === sel ? " selected" : ""}>${esc(f.beginner_name)}${advanced ? ` — ${esc(f.feature_id)}` : ""}${mark}</option>`;
    }).join("")}</optgroup>`).join("");
  }
  function defaultCondition(f) {
    if (f.data_type === "BOOLEAN") return { feature: f.feature_id, op: "is_true" };
    if (f.data_type === "ENUM") return { feature: f.feature_id, op: "==", value: f.values[0].value };
    return { feature: f.feature_id, op: ">=", value: f.minimum != null && f.minimum > 0 ? f.minimum : 0 };
  }
  function valueControl(c, f, dis) {
    if (f.data_type === "BOOLEAN") return `<span class="cc-small cc-dimtext">${esc(c.op === "is_true" ? f.true_text : f.false_text)}</span>`;
    if (f.data_type === "ENUM") {
      if (c.op === "in" || c.op === "not_in") {
        const cur = Array.isArray(c.value) ? c.value : [];
        return `<span class="sl-multi">${f.values.map((v) => `<label><input type="checkbox" data-k="multi" value="${esc(v.value)}"${cur.includes(v.value) ? " checked" : ""}${dis}> ${esc(v.label)}</label>`).join("")}</span>`;
      }
      return `<select data-k="value"${dis}>${f.values.map((v) => `<option value="${esc(v.value)}"${v.value === c.value ? " selected" : ""}>${esc(v.label)}</option>`).join("")}</select>`;
    }
    const unit = f.unit ? `<span class="cc-small cc-dimtext">${esc(f.unit)}</span>` : "";
    const num = (k, v) => `<input type="number" step="any" data-k="${k}" value="${esc(v == null ? "" : v)}"${dis}>`;
    return c.op === "between" ? `${num("lo", (c.value || [])[0])} <span class="cc-small">and</span> ${num("hi", (c.value || [])[1])} ${unit}` : `${num("value", c.value)} ${unit}`;
  }
  function conditionRow(c, side, i, n) {
    const dis = editable() ? "" : " disabled";
    if (c.logic) {                                  // nested groups can only come from the API; shown read-only
      return `<div class="sl-cond sl-nested" data-side="${side}" data-i="${i}"><span class="cc-small">(${esc(c.logic)} group of ${c.conditions.length})</span>
        ${editable() ? `<button class="cc-btn cc-mini cc-link" data-act="remove">Remove</button>` : ""}</div>`;
    }
    const f = byId[c.feature];
    if (!f) return `<div class="sl-cond" data-side="${side}" data-i="${i}"><span class="cc-banner cc-warn">Unknown feature ${esc(c.feature)}</span></div>`;
    const ops = f.allowed_operators.map((o) => `<option value="${esc(o)}"${o === c.op ? " selected" : ""}>${esc(OP_LABEL[o] || o)}</option>`).join("");
    const meta = advanced ? `<div class="sl-meta cc-small cc-dimtext">${esc(f.feature_id)} · ${esc(f.data_type)}${f.unit ? ` · ${esc(f.unit)}` : ""} · ${f.historical_support ? "historical" : f.forward_support ? "forward only" : "not usable yet"} · ${esc(f.source)}</div>` : "";
    return `<div class="sl-cond" data-side="${side}" data-i="${i}">
      <select data-k="feature"${dis}>${featureOptions(c.feature)}</select>
      <select data-k="op"${dis}>${ops}</select>
      <span class="sl-val">${valueControl(c, f, dis)}</span>
      ${editable() ? `<span class="sl-move"><button class="cc-btn cc-mini cc-link" data-act="up" title="Move up"${i === 0 ? " disabled" : ""}>↑</button><button class="cc-btn cc-mini cc-link" data-act="down" title="Move down"${i === n - 1 ? " disabled" : ""}>↓</button>
        <button class="cc-btn cc-mini cc-link" data-act="remove" title="Remove">✕</button></span>` : ""}${meta}</div>`;
  }
  function conditions(side) {
    const g = draft[side], dis = editable() ? "" : " disabled";
    const lead = side === "entry" ? "Enter when" : "Exit when";
    const tail = side === "entry" ? "of these are true:" : "of these happen:";
    return `<div class="sl-rules"><div class="sl-rule-head"><b>${lead}</b> <select data-logic="${side}"${dis}><option value="ALL"${g.logic === "ALL" ? " selected" : ""}>ALL</option><option value="ANY"${g.logic === "ANY" ? " selected" : ""}>ANY</option></select> ${tail}</div>
      ${g.conditions.length ? g.conditions.map((c, i) => conditionRow(c, side, i, g.conditions.length)).join("") : `<p class="cc-small cc-dimtext">No ${side} conditions yet.</p>`}
      ${editable() ? `<button class="cc-btn cc-mini" data-add="${side}">+ Add condition</button>` : ""}</div>`;
  }

  // ---- Stage 3.2: backtests live in backtest_lab.js; the builder only says which saved version is open ----------
  const btRef = () => (mode === "view" && viewing && viewVersion ? { strategy_id: viewing.strategy_id, name: viewing.name,
    version_number: viewVersion.version_number, readiness: viewVersion.readiness, integrity: viewVersion.integrity,
    spec_hash: viewVersion.spec_hash } : null);
  let quietNotify = false;                       // Stage 3.4: openVersion notifies the panels itself (once)
  const btNotify = () => {
    if (quietNotify) return;
    if (window.BacktestLab) window.BacktestLab.select(btRef());
    if (window.ForwardJournal) window.ForwardJournal.select(btRef());       // Stage 3.3: never evaluates anything
  };
  function backtestButton() {
    const ok = viewVersion.readiness === "BACKTEST_READY" && viewVersion.integrity === "OK";
    return `<button class="cc-btn cc-mini" data-top="backtest"${ok ? "" : ` disabled title="Only BACKTEST READY versions can be backtested"`}>Backtest</button>`;
  }
  function forwardButton() {
    const ok = (viewVersion.readiness === "BACKTEST_READY" || viewVersion.readiness === "FORWARD_TEST_ONLY") && viewVersion.integrity === "OK";
    return `<button class="cc-btn cc-mini" data-top="forward"${ok ? "" : ` disabled title="Only saved BACKTEST READY or FORWARD TEST ONLY versions can be forward tested"`}>Forward journal</button>`;
  }

  // ---- builder -------------------------------------------------------------------------------------------------
  function exitExtras() {
    const x = draft.exit, dis = editable() ? "" : " disabled", L = reg.limits;
    const inv = x.invalidation, tgt = x.target;
    const pct = (key, cur, [lo, hi]) => `<input type="number" step="any" min="${lo}" max="${hi}" data-x="${key}-pct" value="${esc(cur && cur.pct != null ? cur.pct : "")}"${dis}> <span class="cc-small cc-dimtext">%</span>`;
    return `<div class="sl-extras">
      <label class="sl-x"><span class="cc-label">INVALIDATION</span><select data-x="invalidation"${dis}>
        <option value=""${!inv ? " selected" : ""}>None</option>
        <option value="CLOSE_BELOW_ENTRY_SUPPORT"${inv && inv.method === "CLOSE_BELOW_ENTRY_SUPPORT" ? " selected" : ""}>Close below entry support</option>
        <option value="PCT_BELOW_ENTRY"${inv && inv.method === "PCT_BELOW_ENTRY" ? " selected" : ""}>Close a % below entry</option></select>
        ${inv && inv.method === "PCT_BELOW_ENTRY" ? pct("invalidation", inv, L.invalidation_pct) : ""}</label>
      <label class="sl-x"><span class="cc-label">TARGET</span><select data-x="target"${dis}>
        <option value=""${!tgt ? " selected" : ""}>None</option>
        <option value="CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"${tgt && tgt.method === "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE" ? " selected" : ""}>Close at entry resistance</option>
        <option value="PCT_ABOVE_ENTRY"${tgt && tgt.method === "PCT_ABOVE_ENTRY" ? " selected" : ""}>Close a % above entry</option></select>
        ${tgt && tgt.method === "PCT_ABOVE_ENTRY" ? pct("target", tgt, L.target_pct) : ""}</label>
      <label class="sl-x"><span class="cc-label">MAXIMUM HOLDING</span><input type="number" step="1" min="${L.max_holding_days[0]}" max="${L.max_holding_days[1]}" data-x="hold" placeholder="none" value="${esc(x.max_holding_days == null ? "" : x.max_holding_days)}"${dis}> <span class="cc-small cc-dimtext">trading days</span></label></div>`;
  }
  function builder() {
    const d = draft, dis = editable() ? "" : " disabled";
    const title = mode === "edit" ? `NEW VERSION OF ${esc(editing.name)} <span class="cc-small cc-dimtext">(based on v${editing.based_on_version})</span>`
      : mode === "view" ? `${esc(viewing.name)} <span class="cc-small cc-dimtext">v${viewVersion.version_number}${viewVersion.version_number === viewing.current_version ? " · current" : ""} · read-only</span>`
        : mode === "example" ? "EXAMPLE RULES" : "NEW STRATEGY";
    const syms = d.universe.symbols;
    return `<section class="cc-card sl-build"><div class="cc-head"><h2>${title}</h2>
        <div class="cc-head-tools">${mode === "view" ? `${backtestButton()}${forwardButton()}<button class="cc-btn cc-mini cc-primary" data-top="newversion">Create new version</button>` : ""}</div></div>
      ${mode === "example" ? `<div class="cc-banner cc-warn sl-example">${esc(reg.example.note)}</div>` : ""}
      <div class="sl-fields"><label><span class="cc-label">STRATEGY NAME</span><input data-f="name" maxlength="80" value="${esc(d.name)}"${dis}></label>
        <label><span class="cc-label">DESCRIPTION (OPTIONAL)</span><input data-f="description" maxlength="500" value="${esc(d.description)}"${dis}></label></div>
      <div class="sl-universe"><span class="cc-label">UNIVERSE · ${syms.length} symbol${syms.length === 1 ? "" : "s"}</span>
        <div class="sl-chips">${syms.map((s) => `<span class="sl-chip">${esc(s)}${editable() ? `<button class="cc-link" data-sym-rm="${esc(s)}" aria-label="Remove ${esc(s)}">×</button>` : ""}</span>`).join("") || `<span class="cc-small cc-dimtext">No symbols yet.</span>`}</div>
        ${editable() ? `<div class="sl-uadd"><input data-f="symbols" placeholder="Add symbols, e.g. AMD, MU, NVDA"> <button class="cc-btn cc-mini" data-top="addsym">Add</button>
          <button class="cc-btn cc-mini" data-top="watchlist">Use current watchlist</button> <button class="cc-btn cc-mini" data-top="holdings">Use my holdings</button></div>
          <div class="cc-small cc-dimtext">Saved versions store this exact list — later watchlist or holdings changes never alter them.</div>` : ""}</div>
      ${conditions("entry")}
      ${conditions("exit")}
      ${exitExtras()}
      <div class="sl-risk"><span class="cc-label">RISK LIMITS (DEFINITION ONLY)</span>
        <label>Max position <input type="number" step="any" data-r="max_position_pct" value="${esc(d.risk.max_position_pct)}"${dis}> <span class="cc-small cc-dimtext">% of strategy equity</span></label>
        <label>Max open positions <input type="number" step="1" data-r="max_open_positions" value="${esc(d.risk.max_open_positions)}"${dis}></label></div>
      ${notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""}
      ${editable() ? `<div class="sl-actions"><button class="cc-btn" data-top="validate">Validate strategy</button>
        <button class="cc-btn cc-primary" data-top="save"${report && report.valid ? "" : " disabled"}>${mode === "edit" ? `Save as v${editing.based_on_version + 1}` : "Save strategy"}</button>
        <button class="cc-btn cc-link" data-top="discard">Discard</button></div>` : ""}</section>`;
  }

  // ---- right column: summary, readiness, (advanced) JSON + hashes, history --------------------------------------
  function side() {
    const r = report;
    const status = !r ? `<p class="cc-small cc-dimtext">Add rules, then validate. Nothing is saved until you press Save.</p>`
      : r.valid ? `<div class="sl-status">${tag("VALID", "ok")} ${readyTag(r.readiness.status)}</div>`
        : `<div class="sl-status">${tag("INVALID", "alert")}</div><ul class="sl-errors">${r.errors.map((e) => `<li><b>${esc(e.code.replace(/_/g, " "))}</b>${e.feature ? ` · ${esc((byId[e.feature] || {}).beginner_name || e.feature)}` : ""}<div class="cc-small">${esc(e.message)}</div><div class="cc-small cc-dimtext">${esc(e.path)}</div></li>`).join("")}</ul>`;
    const why = r && r.valid ? `<div class="cc-label">BACKTEST READINESS — WHY</div><p class="cc-small">${esc(r.readiness.explanation)}</p>
      <ul class="sl-why">${r.readiness.reasons.map((x) => { const s = STATE[x.state]; return `<li class="cc-small"><span class="sl-mark cc-${s[1]}">${s[0]}</span> <b>${esc(x.name)}</b> — ${esc(s[2])}${x.state !== "HISTORICAL" ? `<div class="cc-dimtext">${esc(x.why)}</div>` : ""}</li>`; }).join("")}</ul>
      <p class="cc-small cc-dimtext">Readiness describes data availability only — it is not a quality score.</p>` : "";
    const adv = advanced && r && r.valid ? `<div class="cc-label">CANONICAL SPEC</div><div class="cc-small">spec hash <code>${esc(r.spec_hash)}</code><br>rules hash <code>${esc(r.rules_hash)}</code><br>registry v${esc(r.spec.feature_registry_version)} · <code>${esc(r.spec.feature_registry_fingerprint.slice(0, 16))}…</code></div>
      <pre class="sl-json">${esc(JSON.stringify(r.spec, null, 2))}</pre>` : "";
    return `<section class="cc-card sl-side"><div class="cc-head"><h2>STRATEGY SUMMARY</h2></div>
      <div class="cc-small cc-dimtext">LONG ONLY · DAILY · decisions at the close, actions at the next open</div>
      ${status}${r && r.valid ? `<p class="sl-summary">${esc(r.summary)}</p>` : ""}${why}${adv}
      <div class="sl-nodata">${esc(reg.note)}</div>
      ${viewing ? history() : ""}</section>`;
  }
  function history() {
    const vs = viewing.versions.slice().reverse();
    return `<div class="sl-history"><div class="cc-label">VERSION HISTORY</div>
      ${vs.map((v) => `<div class="sl-ver${viewVersion && v.version_number === viewVersion.version_number ? " sl-on" : ""}">
        <label><input type="checkbox" data-cmp="${v.version_number}"${compareSel.includes(v.version_number) ? " checked" : ""}> <b>v${v.version_number}</b></label>
        ${readyTag(v.readiness)} <span class="cc-small cc-dimtext">${esc(when(v.created_at))} · <code>${esc(v.spec_hash.slice(0, 12))}</code></span>
        ${v.integrity !== "OK" ? tag("INTEGRITY ERROR", "bad") : ""}
        <button class="cc-btn cc-mini cc-link" data-view="${v.version_number}">View</button></div>`).join("")}
      <button class="cc-btn cc-mini" data-top="compare"${compareSel.length === 2 ? "" : " disabled"}>Compare selected</button>
      ${compareOut ? `<div class="sl-compare"><div class="cc-label">v${compareOut.a} → v${compareOut.b}${compareOut.same_rules ? " · same rules" : ""}</div>
        ${compareOut.changes.length ? compareOut.changes.map((c) => `<div class="sl-change"><b>${esc(c.section)} CHANGED</b><div class="cc-small">${esc(typeof c.old === "object" ? JSON.stringify(c.old) : c.old)} → ${esc(typeof c.new === "object" ? JSON.stringify(c.new) : c.new)}</div></div>`).join("") : `<p class="cc-small">No differences.</p>`}
        <p class="cc-small cc-dimtext">${esc(compareOut.note)}</p></div>` : ""}</div>`;
  }

  // ---- left column: saved strategies ---------------------------------------------------------------------------
  function strategies() {
    return `<section class="cc-card sl-list"><div class="cc-head"><h2>SAVED STRATEGIES</h2>
        <label class="cc-small"><input type="checkbox" data-top="archived"${showArchived ? " checked" : ""}> archived</label></div>
      ${list.length ? list.map((s) => `<div class="sl-card${viewing && viewing.strategy_id === s.strategy_id ? " sl-on" : ""}">
        <div class="sl-card-head"><b>${esc(s.name)}</b><span class="cc-small cc-dimtext">v${s.current_version} · current</span></div>
        <div>${readyTag(s.readiness)}${s.archived_at ? ` ${tag("ARCHIVED", "dim")}` : ""}</div>
        <div class="cc-small">${esc(s.universe.slice(0, 6).join(" · "))}${s.universe.length > 6 ? ` · +${s.universe.length - 6}` : ""}</div>
        <div class="cc-small cc-dimtext">Entry ${s.entry_count} condition${s.entry_count === 1 ? "" : "s"} · Exit ${s.exit_count} rule${s.exit_count === 1 ? "" : "s"} · updated ${esc(when(s.updated_at))}</div>
        <div class="sl-card-btns"><button class="cc-btn cc-mini cc-primary" data-open="${esc(s.strategy_id)}">Open</button>
          <button class="cc-btn cc-mini" data-newver="${esc(s.strategy_id)}">Create new version</button>
          <button class="cc-btn cc-mini cc-link" data-archive="${esc(s.strategy_id)}" data-to="${s.archived_at ? "0" : "1"}">${s.archived_at ? "Restore" : "Archive"}</button></div></div>`).join("")
        : `<p class="cc-small cc-dimtext">No saved strategies yet. Build one, validate it and press Save.</p>`}</section>`;
  }
  function registryTable() {
    if (!advanced) return "";
    return `<details class="cc-card sl-registry"><summary><b>FEATURE REGISTRY</b> <span class="cc-small cc-dimtext">v${esc(reg.registry_version)} · ${reg.features.length} features · ${esc(reg.registry_fingerprint.slice(0, 16))}…</span></summary>
      <table class="data-table"><tr><th>Feature</th><th>Type</th><th>Unit</th><th>Operators</th><th>Historical</th><th>Forward</th><th>Source</th><th>Point-in-time</th></tr>
      ${reg.features.map((f) => `<tr><td><b>${esc(f.beginner_name)}</b><div class="cc-small cc-dimtext">${esc(f.feature_id)}</div></td><td>${esc(f.data_type)}</td><td>${esc(f.unit || "")}</td>
        <td class="cc-small">${esc(f.allowed_operators.join(" "))}</td><td>${f.historical_support ? "✓" : "—"}</td><td>${f.forward_support ? "✓" : "—"}</td>
        <td class="cc-small">${esc(f.source)}</td><td class="cc-small">${esc(f.point_in_time)}${f.notes ? ` ${esc(f.notes)}` : ""}</td></tr>`).join("")}</table></details>`;
  }

  // keep keyboard focus on the same control across re-renders (every edit re-validates and redraws)
  const FOCUS_ATTRS = ["data-k", "data-f", "data-r", "data-x", "data-logic", "data-add", "data-top"];
  function focusSelector() {
    const el = document.activeElement;
    if (!el || !root.contains(el)) return null;
    const a = FOCUS_ATTRS.find((x) => el.hasAttribute(x));
    if (!a) return null;
    const row = el.closest(".sl-cond");
    const own = `[${a}="${CSS.escape(el.getAttribute(a))}"]`;
    return row ? `.sl-cond[data-side="${row.dataset.side}"][data-i="${row.dataset.i}"] ${own}` : own;
  }

  function render() {
    const keep = focusSelector();
    root.innerHTML = `<div class="sl">
      <section class="cc-card sl-top"><div class="cc-head"><h2>STRATEGY LAB <span class="cc-small cc-dimtext">explicit rules · versioned</span></h2>
        <div class="cc-head-tools"><div class="mode-toggle"><button data-mode="b" class="${advanced ? "" : "active"}">Beginner</button><button data-mode="a" class="${advanced ? "active" : ""}">Advanced</button></div>
          <button class="cc-btn cc-mini" data-top="new">New strategy</button><button class="cc-btn cc-mini" data-top="example">Load example rules</button></div></div>
        <div class="cc-small cc-dimtext">A defined strategy is not a tested one. Open a saved BACKTEST READY version and press Backtest to see how that exact version behaved on past data, or press Forward journal to record, one completed close at a time, what a saved version's rules say going forward (both shown separately below; no orders). Paper trading comes in a later stage.</div></section>
      <div class="sl-grid">${strategies()}${builder()}${side()}</div>${registryTable()}</div>`;
    wire();
    const el = keep && root.querySelector(keep);
    if (el) el.focus();
  }

  // ---- state changes -------------------------------------------------------------------------------------------
  async function validate() {
    const my = ++seq;
    const r = await send("/api/strategies/validate", { spec: draft });
    if (my !== seq) return;                                   // a newer edit already asked
    report = r.body;
    render();
  }
  function changed() { notice = ""; validate(); }
  function readRow(el) {
    const row = el.closest(".sl-cond"), side = row.dataset.side, i = Number(row.dataset.i);
    return { row, side, i, c: draft[side].conditions[i] };
  }
  function onCondition(el) {
    const { row, side, i, c } = readRow(el);
    const k = el.dataset.k, f = byId[c.feature];
    if (k === "feature") draft[side].conditions[i] = defaultCondition(byId[el.value]);
    else if (k === "op") {
      const op = el.value;
      if (f.data_type === "BOOLEAN") draft[side].conditions[i] = { feature: c.feature, op };
      else if (op === "between") draft[side].conditions[i] = { feature: c.feature, op, value: [typeof c.value === "number" ? c.value : 0, typeof c.value === "number" ? c.value : 0] };
      else if (op === "in" || op === "not_in") draft[side].conditions[i] = { feature: c.feature, op, value: Array.isArray(c.value) ? c.value : [c.value].filter(Boolean) };
      else draft[side].conditions[i] = { feature: c.feature, op, value: Array.isArray(c.value) ? c.value[0] : c.value };
    } else if (k === "value") draft[side].conditions[i].value = f.data_type === "NUMBER" ? (el.value === "" ? null : Number(el.value)) : el.value;
    else if (k === "lo" || k === "hi") {
      const v = Array.isArray(c.value) ? c.value.slice() : [null, null];
      v[k === "lo" ? 0 : 1] = el.value === "" ? null : Number(el.value);
      draft[side].conditions[i].value = v;
    } else if (k === "multi") draft[side].conditions[i].value = [...row.querySelectorAll('[data-k="multi"]:checked')].map((x) => x.value);
    changed();
  }
  const parseSymbols = (t) => String(t || "").split(/[\s,;]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
  function setSymbols(syms, origin) { draft.universe = { type: "EXPLICIT_SYMBOLS", symbols: syms, origin }; changed(); }

  async function open(id, number) {
    const r = await getJSON(`/api/strategies/${encodeURIComponent(id)}`);
    if (r.status !== 200) { notice = r.body.message || "Could not open the strategy."; render(); return; }
    viewing = r.body;
    viewVersion = viewing.versions.find((v) => v.version_number === (number || viewing.current_version));
    draft = clone(viewVersion.spec);
    mode = "view"; editing = null; compareOut = null;
    report = viewVersion.integrity === "OK" ? { valid: true, errors: [], spec: viewVersion.spec, summary: viewVersion.summary,
      spec_hash: viewVersion.spec_hash, rules_hash: viewVersion.rules_hash, readiness: viewVersion.readiness_detail }
      : { valid: false, errors: [{ code: "INTEGRITY_ERROR", path: `v${viewVersion.version_number}`, message: "This stored version failed its hash check and is not trusted." }] };
    render();
    btNotify();
  }
  async function refreshList() {
    const r = await getJSON(`/api/strategies${showArchived ? "?include_archived=true" : ""}`);
    list = (r.body && r.body.strategies) || [];
  }
  async function save() {
    const url = mode === "edit" ? `/api/strategies/${encodeURIComponent(editing.strategy_id)}/versions` : "/api/strategies";
    const body = mode === "edit" ? { spec: draft, based_on_version: editing.based_on_version } : { spec: draft };
    const r = await send(url, body);
    if (r.status !== 201) {
      notice = r.body.message || "Could not save.";
      if (r.body.errors && r.body.errors.length) report = { valid: false, errors: r.body.errors };
      render(); return;
    }
    await refreshList();
    await open(r.body.strategy_id);
  }
  function startVersion(s) {
    const latest = s.versions[s.versions.length - 1];
    draft = clone(latest.spec); mode = "edit"; viewing = s; viewVersion = latest;
    editing = { strategy_id: s.strategy_id, name: s.name, based_on_version: latest.version_number };
    report = null; changed(); btNotify();
  }

  function wire() {
    root.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { advanced = b.dataset.mode === "a"; render(); }));
    root.querySelectorAll(".sl-cond select, .sl-cond input").forEach((el) => el.addEventListener("change", () => onCondition(el)));
    root.querySelectorAll(".sl-cond [data-act]").forEach((b) => b.addEventListener("click", () => {
      const { side, i } = readRow(b), a = draft[side].conditions;
      if (b.dataset.act === "remove") a.splice(i, 1);
      else { const j = b.dataset.act === "up" ? i - 1 : i + 1; [a[i], a[j]] = [a[j], a[i]]; }
      changed();
    }));
    root.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => {
      const first = reg.features.find((f) => f.scope === "STOCK");
      draft[b.dataset.add].conditions.push(defaultCondition(first)); changed();
    }));
    root.querySelectorAll("[data-logic]").forEach((s) => s.addEventListener("change", () => { draft[s.dataset.logic].logic = s.value; changed(); }));
    root.querySelectorAll("[data-f]").forEach((el) => el.addEventListener("change", () => {
      if (el.dataset.f === "symbols") { setSymbols([...draft.universe.symbols, ...parseSymbols(el.value)], draft.universe.origin); return; }
      draft[el.dataset.f] = el.value; changed();
    }));
    root.querySelectorAll("[data-sym-rm]").forEach((b) => b.addEventListener("click", () => setSymbols(draft.universe.symbols.filter((s) => s !== b.dataset.symRm), draft.universe.origin)));
    root.querySelectorAll("[data-x]").forEach((el) => el.addEventListener("change", () => {
      const x = draft.exit, k = el.dataset.x;
      if (k === "invalidation" || k === "target") x[k] = el.value ? (el.value.startsWith("PCT") ? { method: el.value, pct: k === "target" ? 10 : 5 } : { method: el.value }) : null;
      else if (k.endsWith("-pct")) x[k.split("-")[0]] = { ...x[k.split("-")[0]], pct: el.value === "" ? null : Number(el.value) };
      else if (k === "hold") x.max_holding_days = el.value === "" ? null : Number(el.value);
      changed();
    }));
    root.querySelectorAll("[data-r]").forEach((el) => el.addEventListener("change", () => { draft.risk[el.dataset.r] = el.value === "" ? null : Number(el.value); changed(); }));
    root.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => open(b.dataset.open)));
    root.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => open(viewing.strategy_id, Number(b.dataset.view))));
    root.querySelectorAll("[data-newver]").forEach((b) => b.addEventListener("click", async () => {
      const r = await getJSON(`/api/strategies/${encodeURIComponent(b.dataset.newver)}`);
      if (r.status === 200) startVersion(r.body);
    }));
    root.querySelectorAll("[data-archive]").forEach((b) => b.addEventListener("click", async () => {
      await send(`/api/strategies/${encodeURIComponent(b.dataset.archive)}/archive`, { archived: b.dataset.to === "1" });
      await refreshList(); render();
    }));
    root.querySelectorAll("[data-cmp]").forEach((c) => c.addEventListener("change", () => {
      const n = Number(c.dataset.cmp);
      compareSel = c.checked ? [...compareSel.filter((x) => x !== n), n].slice(-2) : compareSel.filter((x) => x !== n);
      render();
    }));
    root.querySelectorAll("[data-top]").forEach((b) => b.addEventListener(b.type === "checkbox" ? "change" : "click", async () => {
      const t = b.dataset.top;
      if (t === "new") { draft = blank(); mode = "new"; editing = null; viewing = null; report = null; compareSel = []; compareOut = null; notice = ""; render(); btNotify(); }
      else if (t === "example") { draft = clone(reg.example.spec); mode = "example"; editing = null; viewing = null; compareOut = null; changed(); btNotify(); }
      else if (t === "backtest") { if (window.BacktestLab && btRef()) window.BacktestLab.open(btRef()); }
      else if (t === "forward") { if (window.ForwardJournal && btRef()) window.ForwardJournal.open(btRef()); }
      else if (t === "validate") validate();
      else if (t === "save") save();
      else if (t === "discard") { if (viewing) open(viewing.strategy_id); else { draft = blank(); mode = "new"; report = null; render(); } }
      else if (t === "newversion") startVersion(viewing);
      else if (t === "addsym") { const inp = root.querySelector('[data-f="symbols"]'); setSymbols([...draft.universe.symbols, ...parseSymbols(inp.value)], draft.universe.origin); }
      else if (t === "watchlist" || t === "holdings") {           // explicit, read-only copy into the builder
        const r = await getJSON(`/api/strategies/shortcuts/${t}`);
        if (r.body.available && r.body.symbols.length) setSymbols(r.body.symbols, r.body.origin);
        else { notice = r.body.message || "No symbols were found."; render(); }
      } else if (t === "archived") { showArchived = b.checked; await refreshList(); render(); }
      else if (t === "compare" && compareSel.length === 2) {
        const [a, bb] = compareSel.slice().sort((x, y) => x - y);
        const r = await getJSON(`/api/strategies/${encodeURIComponent(viewing.strategy_id)}/compare?a=${a}&b=${bb}`);
        compareOut = r.status === 200 ? r.body : { a, b: bb, changes: [], note: r.body.message || "Could not compare." };
        render();
      }
    }));
  }

  let loaded = false, loading = null;
  async function load() {
    if (loading) return loading;                  // one load at a time (Strategy Fit's links may wait for it)
    loading = (async () => {
      root.innerHTML = `<p class="hint">Loading the Strategy Lab…</p>`;
      const r = await getJSON("/api/strategies/features");
      if (r.status !== 200) { root.innerHTML = `<div class="cc-banner cc-warn">The Strategy Lab could not load. If the server was started before this update, restart it.</div>`; return; }
      reg = r.body; byId = Object.fromEntries(reg.features.map((f) => [f.feature_id, f]));
      await refreshList();
      draft = blank();
      loaded = true;
      render();
    })().finally(() => { loading = null; });
    return loading;
  }
  const tabBtn = document.querySelector('.tab-btn[data-tab="strategy"]');
  if (tabBtn) tabBtn.addEventListener("click", () => { if (!loaded) load(); });
  // Stage 3.4: Strategy Fit's evidence links open that exact saved version here, then its Backtest / Forward panel
  async function openVersion(id, number, panel, runId) {
    if (!loaded) await load();
    if (!loaded) return;                          // the Lab could not load (its own banner says so)
    quietNotify = true;
    try { await open(id, number); } finally { quietNotify = false; }
    const ref = btRef();
    const bt = window.BacktestLab ? window.BacktestLab[panel === "backtest" && ref ? "open" : "select"](ref) : null;
    if (window.ForwardJournal) window.ForwardJournal[panel === "forward" && ref ? "open" : "select"](ref);
    // Stage 3.5 Evidence: "Open historical equity curve" opens that exact stored run with the panel's own Open button
    if (panel === "backtest" && bt && /^[0-9a-f]{32}$/.test(runId || "")) {
      await bt;
      const b = document.querySelector(`#bt-body [data-bt-open="${runId}"]`);
      if (b) b.click();
    }
  }
  window.StrategyLab = { load, openVersion, get state() { return { mode, draft, report, list }; } };
})();
