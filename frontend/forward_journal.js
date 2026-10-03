// forward_journal.js — Stage 3.3 FORWARD JOURNAL panel inside the Strategy Lab tab.
//
// Records, one completed market close at a time and only when the user asks, what one SAVED strategy version's rules
// say going forward. No broker, no orders, no sizing, no scheduler, no Claude. Server calls: the version's journals
// (+ eligibility) when the panel is opened, one stored journal, "Start forward journal", an explicit preflight, an
// explicit "Record", "Archive", and stored session details (cached here: opening a session twice costs no request).
// After a recording only the affected parts of the panel are redrawn — never the Strategy Lab.
// Stage 3.6: stored forward MFE / MAE of reference cycles (observed going forward with each Record; never backfilled —
// cycles that started earlier are shown as legacy, not tracked). No extra request or button: it comes with the journal.
(function () {
  const root = document.getElementById("fj-body");
  if (!root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const getJSON = (u) => fetch(u).then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const send = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) })
    .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const money = (v) => (isNum(v) ? `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—");
  const pct = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%` : "—");
  const short = (h) => (h ? String(h).slice(0, 12) : "—");
  const day = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "—");
  const when = (v) => (v ? new Date(v).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const words = (s) => String(s || "").replace(/_/g, " ");
  const val = (v) => (v == null ? "—" : isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : Array.isArray(v) ? v.join(", ") : String(v));
  const fval = (v, unit, label) => (label != null ? label : !isNum(v) ? val(v) : unit === "%" ? pct(v) : unit === "$" ? money(v)
    : unit === "x" ? `${v.toFixed(2)}x` : unit === "0-100" ? v.toFixed(1) : val(v));
  const INTEGRITY = new Set(["STRATEGY_FOUND", "VERSION_FOUND", "VERSION_IMMUTABLE", "SPEC_HASH_VERIFIED", "RULES_HASH_VERIFIED", "SCHEMA_VERSION",
    "REGISTRY_VERSION", "REGISTRY_FINGERPRINT", "SPEC_STILL_VALID", "READINESS_CONSISTENT", "FORWARD_SUPPORT", "SESSION_COMPLETE_RULE"]);

  const STATUS_TAG = { ACTIVE: "ok", CONTINUITY_BLOCKED: "alert", ARCHIVED: "dim" };
  const CONT_TAG = { CONTINUOUS: "ok", GAPPED: "warn", NOT_STARTED: "dim" };
  const STATE_TEXT = { FLAT: "Flat", ENTRY_PENDING: "Entry pending", OPEN: "Open — signal journal", EXIT_PENDING: "Exit pending", CONTINUITY_BLOCKED: "Continuity blocked" };
  const REASON = {
    ENTRY_RULES_MET: "The strategy's entry rules were met.", EXIT_RULES_MET: "The strategy's exit rules were met.",
    NO_ENTRY: "Entry rules not met.", NO_EXIT_SIGNAL: "Exit rules not met.",
    REQUIRED_DATA_UNAVAILABLE: "A rule needed data that was unavailable, so the outcome could not be decided.",
    REQUIRED_ENTRY_SUPPORT_UNAVAILABLE: "Entry rules met, but the entry-time support level the exit rule needs was unavailable.",
    REQUIRED_ENTRY_RESISTANCE_UNAVAILABLE: "Entry rules met, but the entry-time resistance level the exit rule needs was unavailable.",
    NO_BAR_FOR_SESSION: "No daily bar for this symbol on this session — not evaluated.",
    NOT_EVALUATED: "Not enough bars to evaluate this symbol on this session.",
    FORWARD_CONTINUITY_GAP: "A session was missed while this symbol's shadow state was pending or open — no further lifecycle claims.",
  };
  const XTAG = { TRACKING: ["COMPLETE SO FAR", "info"], COMPLETE: ["TRACKED", "ok"], LEGACY_NOT_TRACKED: ["LEGACY · NOT TRACKED", "dim"],
    INCOMPLETE_DUE_TO_CONTINUITY_GAP: ["INCOMPLETE · CONTINUITY GAP", "alert"], INCOMPLETE_DATA_UNAVAILABLE: ["INCOMPLETE · DATA UNAVAILABLE", "warn"] };
  const PF_TAG = { READY: ["READY TO RECORD", "ok"], CAN_RECORD_WITH_MISSING_INPUT: ["CAN RECORD WITH MISSING INPUT", "warn"],
    ALREADY_RECORDED: ["ALREADY RECORDED", "info"], NO_ELIGIBLE_SESSION: ["NO ELIGIBLE SESSION YET", "dim"],
    SESSION_NOT_COMPLETE: ["SESSION NOT COMPLETE", "dim"], BLOCKED: ["BLOCKED", "bad"], JOURNAL_ARCHIVED: ["ARCHIVED", "dim"],
    DATA_UNAVAILABLE: ["DATA UNAVAILABLE", "bad"] };

  let sel = null, open = false, list = null, view = null, pf = null, busy = "", notice = "", confirmStart = false,
    confirmArchive = false, detail = null, detailSym = "";
  const cache = new Map();                   // "journal|date" -> stored session view (never recomputed)

  function decisionLabel(o) {
    if (o.state_after === "CONTINUITY_BLOCKED") return ["CONTINUITY BLOCKED", "alert"];
    if (o.decision === "ENTER") return ["ENTER", "info"];
    if (o.decision === "EXIT") return ["EXIT", "info"];
    if (o.decision === "SKIP") return ["SKIP", "warn"];
    return o.state_after === "OPEN" ? ["HOLD / NO EXIT SIGNAL", "dim"] : ["NO ENTRY", "dim"];
  }

  // ---- shell + parts (each part is redrawn on its own) ------------------------------------------------------------
  function shell() {
    root.innerHTML = `<section class="cc-card fj"><div class="cc-head"><h2>FORWARD JOURNAL</h2>
        <div class="cc-head-tools"><span class="fj-strip">No broker · No orders · Manual capture only</span>
          <button class="cc-btn cc-mini cc-link" data-fj="close">Close</button></div></div>
      <div data-part="automation"></div><div data-part="status"></div><div data-part="cycles"></div><div data-part="action"></div><div data-part="latest"></div>
      <div data-part="history"></div><div data-part="detail"></div><div data-part="archived"></div></section>`;
    root.querySelector('[data-fj="close"]').addEventListener("click", () => { open = false; root.innerHTML = ""; });
    // Stage 3.7: the global, opt-in AUTOMATIC CAPTURE section (its own script; it never redraws this panel)
    if (window.ForwardAutomation) window.ForwardAutomation.mount(root.querySelector('[data-part="automation"]'));
  }
  function part(name, html) {
    const el = root.querySelector(`[data-part="${name}"]`);
    if (!el) return;
    el.innerHTML = html;
    wire(el);
  }
  function drawAll() { drawStatus(); drawCycles(); drawAction(); drawLatest(); drawHistory(); drawDetail(); drawArchived(); }

  function sources() {
    const e = (list && list.eligibility) || {};
    const fo = e.forward_only_features || [];
    const bt = e.historical_backtest_available
      ? `${tag("AVAILABLE", "ok")} <span class="cc-small cc-dimtext">BACKTEST READY</span>`
      : `${tag("UNAVAILABLE FOR THESE RULES", "dim")}${fo.length ? `<div class="cc-small cc-dimtext">Uses data that cannot be rebuilt for past dates: ${fo.map((f) => `<b>${esc(f.name)}</b>`).join(", ")}.</div>` : ""}`;
    const fw = e.eligible ? tag("AVAILABLE", "ok")
      : `${tag("UNAVAILABLE", "bad")} <span class="cc-small">${esc(((e.errors || [])[0] || {}).message || "")}</span>`;
    return `<div class="fj-sources"><div><span class="cc-label">HISTORICAL BACKTEST</span><div>${bt}</div></div>
      <div><span class="cc-label">FORWARD JOURNAL</span><div>${fw}</div></div>
      <div class="cc-small cc-dimtext">Independent evidence sources — they are not merged.</div></div>`;
  }

  function drawStatus() {
    if (!view) {
      const e = (list && list.eligibility) || {};
      const today = new Date().toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
      part("status", `${meta(sel.name, sel.version_number, sel.spec_hash, null)}${sources()}
        ${e.eligible ? `<div class="fj-start">${confirmStart ? `<div class="cc-confirm"><b>This creates a forward-only record from future sessions. Past sessions cannot be added later.</b>
            <div class="cc-small">The first eligible session is the first market session after today (${esc(today)}), even if you start before today's close. Nothing is evaluated until you press Record.</div>
            <div class="cc-actions"><button class="cc-btn cc-primary" data-fj="start"${busy ? " disabled" : ""}>${busy === "starting" ? "Starting…" : "Start"}</button><button class="cc-btn cc-link" data-fj="cancelstart">Cancel</button></div></div>`
          : `<button class="cc-btn cc-primary" data-fj="askstart">Start forward journal</button> <span class="cc-small cc-dimtext">No market data is read and nothing is evaluated when a journal starts.</span>`}</div>` : ""}`);
      return;
    }
    const j = view.journal, s = view.summary;
    const first = s.first_eligible_session ? day(s.first_eligible_session) : `first session on or after ${day(j.forward_start_date)}`;
    part("status", `${meta(j.name, j.version_number, j.spec_hash, j.created_at)}${sources()}
      <div class="fj-grid">
        <div><span class="cc-label">STATUS</span><div>${tag(words(j.status), STATUS_TAG[j.status] || "dim")}</div></div>
        <div><span class="cc-label">CONTINUITY</span><div>${tag(words(s.continuity), CONT_TAG[s.continuity] || "dim")}</div></div>
        <div><span class="cc-label">FIRST ELIGIBLE</span><div>${esc(first)}</div></div>
        <div><span class="cc-label">LAST CAPTURED</span><div>${esc(day(s.latest_captured_session))}</div></div>
        <div><span class="cc-label">SESSIONS</span><div>${esc(s.sessions_captured)} captured · ${esc(s.sessions_missed)} missed</div></div>
      </div>
      <div class="fj-counts cc-small">
        <span>ENTER signals <b>${esc(s.enter_signals)}</b></span><span>EXIT signals <b>${esc(s.exit_signals)}</b></span>
        <span>SKIP decisions <b>${esc(s.skip_decisions)}</b></span><span>Open shadow states <b>${esc(s.open_shadow_states)}</b></span>
        <span>Pending reference fills <b>${esc(s.pending_entries + s.pending_exits)}</b></span>
        <span>Completed reference cycles <b>${esc(s.completed_reference_cycles)}</b></span>
        ${s.completed_reference_cycles ? `<span title="${esc(s.average_move_note)}">Average completed reference move <b>${esc(pct(s.average_completed_reference_move_pct))}</b> <span class="cc-dimtext">(descriptive)</span></span>` : ""}
        ${s.blocked_symbols.length ? `<span>Blocked <b>${esc(s.blocked_symbols.join(", "))}</b></span>` : ""}
      </div>
      ${j.status === "CONTINUITY_BLOCKED" ? `<div class="cc-banner cc-warn">Some symbols are CONTINUITY BLOCKED: a session was missed while their shadow state was pending or open, so their lifecycle cannot be known. Other symbols keep being observed. To observe them again, end this journal and start a new one.</div>` : ""}`);
  }
  function meta(name, n, hash, created) {
    return `<div class="fj-meta"><div><span class="cc-label">STRATEGY</span><div><b>${esc(name)}</b></div></div>
      <div><span class="cc-label">VERSION</span><div>v${esc(n)} · <code>${esc(short(hash))}</code></div></div>
      ${created ? `<div><span class="cc-label">CREATED</span><div>${esc(when(created))}</div></div>` : ""}</div>`;
  }

  function drawAction() {
    if (!view) { part("action", notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""); return; }
    const j = view.journal;
    if (j.status === "ARCHIVED") {
      part("action", `<div class="cc-banner">This journal is archived (${esc(when(j.archived_at))}). Its observations stay viewable; it records nothing more.</div>
        ${notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""}${!primary() && list && list.eligibility && list.eligibility.eligible ? `<button class="cc-btn cc-mini" data-fj="new">Start a new journal for this version</button>` : ""}`);
      return;
    }
    const canRecord = pf && (pf.status === "READY" || pf.status === "CAN_RECORD_WITH_MISSING_INPUT") && !busy;
    part("action", `<div class="fj-actions">
        <button class="cc-btn cc-primary" data-fj="check"${busy ? " disabled" : ""}>${busy === "checking" ? "Checking…" : "Record latest completed close"}</button>
        ${confirmArchive ? `<span class="cc-confirm fj-inline">End this journal? It stops future captures; nothing is deleted.
            <button class="cc-btn cc-mini" data-fj="archive">End journal</button><button class="cc-btn cc-mini cc-link" data-fj="cancelarchive">Cancel</button></span>`
          : `<button class="cc-btn cc-mini cc-link" data-fj="askarchive">End this journal</button>`}</div>
      ${notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""}
      ${pf ? preflightView(canRecord) : `<div class="cc-small cc-dimtext">Nothing is recorded automatically. Each capture reads the latest completed close only; a session you do not record becomes a MISSED session and is never filled in later.</div>`}`);
  }
  function integrity(checks, ck) {
    const ic = checks.filter((c) => INTEGRITY.has(c.code));
    if (!ic.length) return "";
    const bad = ic.filter((c) => c.ok === false);
    return `<details class="fj-integrity"${bad.length ? " open" : ""}><summary class="cc-small"><span class="fj-mark ${bad.length ? "cc-bad" : "cc-ok"}">${bad.length ? "✕" : "✓"}</span> Strategy version ${bad.length ? `failed ${esc(bad.length)} of ${esc(ic.length)} integrity checks` : `verified — ${esc(ic.length)} integrity checks passed`} <span class="cc-dimtext">(hashes, immutability, registry, readiness)</span></summary>
      <ul class="fj-checks">${ic.map(ck).join("")}</ul></details>`;
  }
  function preflightView(canRecord) {
    const t = PF_TAG[pf.status] || [pf.status, "dim"];
    const s = pf.session || {};
    const ck = (c) => `<li class="cc-small"><span class="fj-mark ${c.ok === false ? "cc-bad" : c.ok ? "cc-ok" : "cc-warn"}">${c.ok === false ? "✕" : c.ok ? "✓" : "⚠"}</span> ${esc(c.label)}${c.detail ? ` <span class="cc-dimtext">${esc(c.detail)}</span>` : ""}</li>`;
    return `<div class="fj-pre"><div class="fj-pre-head">${tag(t[0], t[1])}${s.latest_completed ? ` <span>Latest completed close: <b>${esc(day(s.latest_completed))}</b></span>` : ""}
        ${pf.continuity ? ` ${tag(words(pf.continuity.status), CONT_TAG[pf.continuity.status] || "dim")}` : ""}</div>
      ${(pf.errors || []).map((e) => `<div class="cc-banner cc-warn"><b>${esc(words(e.code))}</b> — ${esc(e.message)}</div>`).join("")}
      ${integrity(pf.checks || [], ck)}<ul class="fj-checks">${(pf.checks || []).filter((c) => !INTEGRITY.has(c.code)).map(ck).join("")}</ul>
      ${pf.download ? `<div class="cc-small">Recording will download ${esc(pf.download.count)} symbol(s) of daily bars (~${esc(pf.download.estimated_bars.toLocaleString())} bars, one read-only market-data request).</div>` : ""}
      ${(pf.pending || []).length ? `<div class="cc-small">Pending reference fills that this session resolves: ${pf.pending.map((p) => `<b>${esc(p.symbol)}</b> ${esc(words(p.state).toLowerCase())} (signal ${esc(day(p.signal_session))})`).join(", ")}</div>` : ""}
      ${(pf.warnings || []).length ? `<ul class="fj-warns">${pf.warnings.map((w) => `<li><b>${esc(words(w.code))}</b><div class="cc-small">${esc(w.text)}</div></li>`).join("")}</ul>` : ""}
      ${pf.status === "ALREADY_RECORDED" ? `<button class="cc-btn cc-mini" data-fj-open="${esc(s.latest_completed)}">Open the stored session</button>` : ""}
      ${canRecord ? `<div class="cc-actions"><button class="cc-btn cc-primary" data-fj="record">${busy === "recording" ? "Recording…" : `Record ${esc(day(s.latest_completed))} close`}</button>
        <span class="cc-small cc-dimtext">Evaluates the saved rules on this close and stores the observation permanently. No order is placed.</span></div>` : ""}</div>`;
  }

  // ---- latest session + symbol cards -------------------------------------------------------------------------------
  function leafRows(trace, depth) {
    return (trace || []).map((t) => {
      if (t.children) return `<li class="fj-grp">${esc(t.group === "ALL" ? "All of" : "Any of")}:<ul>${leafRows(t.children, depth + 1)}</ul></li>`;
      const m = t.result === "MET" ? ["✓", "cc-ok"] : t.result === "NOT_MET" ? ["✕", "cc-dim"] : ["?", "cc-warn"];
      return `<li><span class="fj-mark ${m[1]}">${m[0]}</span> ${esc(t.text || t.feature)} <span class="cc-dimtext">— ${esc(t.result === "UNAVAILABLE" ? `unavailable (${words(t.reason || t.availability).toLowerCase()})` : `actual ${fval(t.actual, t.unit, t.actual_label)}`)}</span></li>`;
    }).join("");
  }
  function exitRows(tr) {
    const rows = [];
    if (tr.conditions) rows.push(leafRows(tr.conditions.trace, 0));
    const lvl = (x, label) => {
      const m = x.hit === true ? ["✓", "cc-ok"] : x.hit === false ? ["✕", "cc-dim"] : ["?", "cc-warn"];
      return `<li><span class="fj-mark ${m[1]}">${m[0]}</span> ${esc(label)} <span class="cc-dimtext">— level ${esc(money(x.level))}, close ${esc(money(x.close))}</span></li>`;
    };
    if (tr.invalidation) rows.push(lvl(tr.invalidation, tr.invalidation.method === "CLOSE_BELOW_ENTRY_SUPPORT" ? "Close below entry support" : "Close at or below the % invalidation level"));
    if (tr.target) rows.push(lvl(tr.target, tr.target.method === "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE" ? "Close reaches entry resistance" : "Close at or above the % target level"));
    if (tr.max_holding) rows.push(`<li><span class="fj-mark ${tr.max_holding.hit ? "cc-ok" : "cc-dim"}">${tr.max_holding.hit ? "✓" : "✕"}</span> Maximum holding reached <span class="cc-dimtext">— ${esc(tr.max_holding.holding_sessions)} of ${esc(tr.max_holding.max_holding_days)} captured sessions</span></li>`);
    return rows.join("");
  }
  function fillsFor(sess, sym) {
    return [...(sess.fills_resolved || []), ...(sess.fills_from_signals || [])].filter((f, i, a) => f.symbol === sym && a.findIndex((g) => g.symbol === f.symbol && g.cycle_no === f.cycle_no && g.fill_type === f.fill_type) === i);
  }
  function refBlock(o, sess) {
    const lc = o.lifecycle || {};
    const fl = fillsFor(sess, o.symbol).filter((f) => f.resolved_in_session === sess.session.session_date);
    const out = [];
    fl.forEach((f) => {
      if (f.fill_type === "ENTRY") out.push(f.status === "FILLED" ? `Reference entry <b>${esc(money(f.reference_open_price))}</b> at the ${esc(day(f.fill_session_date))} open` : `Reference entry UNFILLED — ${esc(words(f.reason_code).toLowerCase())}`);
      else out.push(`Reference exit <b>${esc(money(f.reference_open_price))}</b> at the ${esc(day(f.fill_session_date))} open · reference entry ${esc(money(f.reference_entry_price))} · reference move <b>${esc(pct(f.reference_move_pct))}</b> <span class="cc-dimtext">(descriptive)</span>`);
    });
    if (o.state_after === "ENTRY_PENDING") out.push("Reference fill: pending — next session's open");
    if (o.state_after === "EXIT_PENDING") out.push("Reference exit: pending — next session's open");
    if (o.state_after === "OPEN" && !fl.length) out.push(`Reference entry ${esc(money(lc.reference_entry_open))} (${esc(day(lc.entry_fill_session))})`);
    if ((o.state_after === "OPEN" || o.state_after === "EXIT_PENDING") && lc.holding_sessions) out.push(`Current journal age: ${esc(lc.holding_sessions)} captured trading session${lc.holding_sessions === 1 ? "" : "s"}`);
    const ex = excursionBlock(o, sess, fl);
    return out.length || ex ? `${out.length ? `<div class="fj-ref"><span class="cc-label">REFERENCE FILL</span>${out.map((x) => `<div class="cc-small">${x}</div>`).join("")}</div>` : ""}${ex}` : "";
  }
  // Stage 3.6: MFE / MAE as stored at THIS session (current while open, final at the reference exit open)
  function cycleStatus(sym, cyc) {
    const c = view && view.excursions ? view.excursions.cycles.find((x) => x.symbol === sym && x.cycle_no === cyc) : null;
    return c ? c.status : null;
  }
  function excursionBlock(o, sess, fl) {
    const rows = (sess.excursions || []).filter((x) => x.symbol === o.symbol);
    const lc = o.lifecycle || {};
    const lines = [];
    const exitFill = fl.find((f) => f.fill_type === "EXIT" && f.status === "FILLED");
    if (exitFill) {
      const r = rows.find((x) => x.cycle_no === exitFill.cycle_no && x.observation_type === "EXIT_OPEN");
      if (r) lines.push(`Cycle ${esc(exitFill.cycle_no)} final: MFE <b>${esc(pct(r.cumulative_mfe_pct))}</b> · MAE <b>${esc(pct(r.cumulative_mae_pct))}</b> <span class="cc-dimtext">(reference entry to the exit open; descriptive)</span>`);
      else if (cycleStatus(o.symbol, exitFill.cycle_no) === "LEGACY_NOT_TRACKED") lines.push("MFE / MAE: Not tracked for this legacy forward cycle");
    }
    if (o.state_after === "OPEN" || o.state_after === "EXIT_PENDING") {
      const r = rows.find((x) => x.cycle_no === lc.cycle_no);
      if (r) {
        const t = XTAG[r.tracking_status] || [words(r.tracking_status), "dim"];
        lines.push(`<span class="cc-label">FORWARD JOURNAL STATE</span>`);
        lines.push(`Reference entry <b>${esc(money(r.entry_reference_price))}</b> (${esc(day(r.entry_session_date))})`);
        lines.push(`Current tracked MFE <b>${esc(pct(r.cumulative_mfe_pct))}</b> · Current tracked MAE <b>${esc(pct(r.cumulative_mae_pct))}</b>`);
        lines.push(`Tracking: ${tag(t[0], t[1])} <span class="cc-dimtext">not final while the cycle is open</span>${r.status === "EXCURSION_DATA_UNAVAILABLE" ? ` <span class="cc-dimtext">· this session: ${esc(words(r.reason_code).toLowerCase())}</span>` : ""}`);
      } else if (cycleStatus(o.symbol, lc.cycle_no) === "LEGACY_NOT_TRACKED") lines.push("MFE / MAE: Not tracked for this legacy forward cycle");
    }
    const gap = rows.find((x) => x.observation_type === "CONTINUITY_GAP");
    if (gap) lines.push(`MFE / MAE tracking incomplete due to continuity gap <span class="cc-dimtext">(cycle ${esc(gap.cycle_no)}; last observed MFE ${esc(pct(gap.cumulative_mfe_pct))} · MAE ${esc(pct(gap.cumulative_mae_pct))} — not final)</span>`);
    return lines.length ? `<div class="fj-ref fj-exc">${lines.map((x) => `<div class="cc-small">${x}</div>`).join("")}</div>` : "";
  }
  function card(o, sess) {
    const [t, k] = decisionLabel(o);
    const side = o.evaluated_side;
    const tr = o.evaluation_trace;
    const conds = tr ? (side === "EXIT" ? exitRows(tr) : leafRows(tr.trace, 0)) : "";
    const why = o.decision === "EXIT" ? `<div class="cc-label">WHY</div><ul class="fj-conds">${o.exit_reasons.map((r) => `<li><span class="fj-mark cc-ok">✓</span> ${esc({ CONDITION: "Exit condition met", INVALIDATION: "Invalidation level reached", TARGET: "Target level reached", MAX_HOLDING: "Maximum holding reached" }[r] || r)}</li>`).join("")}</ul>` : "";
    return `<div class="fj-card fj-k-${esc(k)}"><div class="fj-card-head"><b class="fj-sym">${esc(o.symbol)}</b>${tag(t, k)}</div>
      <div class="cc-small">${esc(REASON[o.reason_code] || words(o.reason_code))}${o.decision === "ENTER" || o.decision === "EXIT" ? " No order was placed." : ""}</div>
      <div class="cc-small cc-dimtext">State ${esc(STATE_TEXT[o.state_before] || o.state_before)} → <b>${esc(STATE_TEXT[o.state_after] || o.state_after)}</b></div>
      ${why}
      ${o.rules_total != null ? `<div class="cc-label">${side === "EXIT" ? "EXIT RULES" : "ENTRY CONDITIONS"} — ${esc(o.rules_met)} / ${esc(o.rules_total)} ${side === "EXIT" ? "currently met" : "met"}</div><ul class="fj-conds">${conds}</ul>` : ""}
      ${refBlock(o, sess)}
      <button class="cc-btn cc-mini cc-link" data-fj-open="${esc(sess.session.session_date)}" data-fj-sym="${esc(o.symbol)}">Details</button></div>`;
  }
  function sessionSummary(sess) {
    const s = sess.session, sm = s.summary || {};
    const g = sm.groups || {};
    const line = (label, key) => `<div><span class="cc-label">${label}</span><div>${(g[key] || []).length ? esc(g[key].join(", ")) : "none"}</div></div>`;
    return `<div class="fj-sum"><div><span class="cc-label">SESSION</span><div><b>${esc(day(s.session_date))}</b></div></div>
      <div><span class="cc-label">COVERAGE</span><div>${esc(sm.coverage_text || "")}</div></div>
      ${line("ENTER", "ENTER")}${line("EXIT", "EXIT")}${line("HOLD / NO EXIT SIGNAL", "HOLD")}${line("NO ENTRY", "NO_ENTRY")}${line("SKIPPED", "SKIPPED")}
      ${(g.BLOCKED || []).length ? line("CONTINUITY BLOCKED", "BLOCKED") : ""}
      <div><span class="cc-label">CONTEXT TIMING</span><div>${s.context_timing === "STRICT_FORWARD" ? tag("STRICT FORWARD", "ok") : tag("POST-CLOSE FORWARD CONTEXT", "warn")}</div></div></div>`;
  }
  function drawLatest() {
    const sess = view && view.latest_session;
    if (!sess) { part("latest", view ? `<div class="cc-small cc-dimtext fj-empty">No session captured yet.</div>` : ""); return; }
    part("latest", `<div class="fj-latest"><h3>LATEST SESSION — ${esc(day(sess.session.session_date).toUpperCase())}</h3>${sessionSummary(sess)}
      <div class="fj-cards">${sess.observations.map((o) => card(o, sess)).join("")}</div>
      <div class="cc-small cc-dimtext">These are forward observations of the rules, not recommendations. ENTER / EXIT are strategy decisions; no order was placed.</div></div>`);
  }

  // ---- Stage 3.6: reference cycles with stored MFE / MAE -------------------------------------------------------------
  function drawCycles() {
    const x = view && view.excursions;
    if (!x || (!x.cycles.length && !x.tracking)) { part("cycles", ""); return; }
    const since = x.tracking ? `MFE / MAE tracked from the ${esc(day(x.tracking.activated_in_session))} capture onward. Cycles entered earlier are legacy and not tracked.`
      : "MFE / MAE tracking starts with the next recorded session. The cycles below started earlier and are legacy (not tracked).";
    const m = x.metrics;
    const exc = (c) => {
      if (c.status === "LEGACY_NOT_TRACKED") return `<td colspan="2" class="cc-dimtext">Not tracked for this legacy forward cycle</td>`;
      if (c.status === "INCOMPLETE_DUE_TO_CONTINUITY_GAP" || c.status === "INCOMPLETE_DATA_UNAVAILABLE") return `<td colspan="2" class="cc-dimtext">Incomplete — no final MFE / MAE</td>`;
      const so = c.final ? "" : ` <span class="cc-dimtext">so far</span>`;
      return `<td>${esc(pct(c.mfe_pct))}${so}</td><td>${esc(pct(c.mae_pct))}${so}</td>`;
    };
    const rows = x.cycles.map((c) => {
      const t = XTAG[c.status] || [words(c.status), "dim"];
      return `<tr><td><b>${esc(c.symbol)}</b></td><td>${esc(c.cycle_no)}</td><td>${esc(day(c.reference_entry_session))} · ${esc(money(c.reference_entry_open))}</td>
        <td>${c.completed ? `${esc(day(c.reference_exit_session))} · ${esc(money(c.reference_exit_open))}` : `<span class="cc-dimtext">open</span>`}</td>
        <td>${c.completed ? esc(pct(c.reference_move_pct)) : "—"}</td>${exc(c)}<td>${tag(t[0], t[1])}</td></tr>`;
    }).join("");
    part("cycles", `<div class="fj-cycles"><h3>REFERENCE CYCLES · MFE / MAE <span class="cc-small cc-dimtext">stored forward observations · descriptive</span></h3>
      <div class="cc-small cc-dimtext">${since}</div>
      ${x.cycles.length ? `<div class="fj-tablewrap"><table class="data-table fj-cyc"><tr><th>Symbol</th><th>Cycle</th><th>Reference entry</th><th>Reference exit</th><th>Reference move</th><th>MFE</th><th>MAE</th><th>Tracking</th></tr>${rows}</table></div>` : `<div class="cc-small cc-dimtext">No reference entry has filled yet.</div>`}
      ${m.completed_cycles ? `<div class="fj-counts cc-small"><span>MFE / MAE sample <b>${esc(m.tracked_completed_cycles)}</b> tracked of <b>${esc(m.completed_cycles)}</b> completed cycles</span>
        ${m.tracked_completed_cycles ? `<span>Average MFE <b>${esc(pct(m.average_mfe_pct))}</b></span><span>Average MAE <b>${esc(pct(m.average_mae_pct))}</b> <span class="cc-dimtext">(tracked completed cycles only; descriptive)</span></span>` : ""}
        ${m.legacy_untracked_completed_cycles ? `<span class="cc-dimtext">${esc(m.legacy_untracked_completed_cycles)} earlier cycle${m.legacy_untracked_completed_cycles === 1 ? " was" : "s were"} not MFE / MAE tracked</span>` : ""}</div>` : ""}
      <div class="cc-small cc-dimtext">${esc(x.note)} Open cycles show values so far and are not part of any average. No sizing, dollars or costs.</div></div>`);
  }

  // ---- history ------------------------------------------------------------------------------------------------------
  function drawHistory() {
    if (!view || !view.timeline.length) { part("history", ""); return; }
    const rows = view.timeline.map((r) => {
      if (r.kind === "MISSED") return `<div class="fj-row fj-missed"><span class="fj-date fj-date-text">${esc(day(r.session_date))}</span>${tag("MISSED SESSION", "warn")}<span class="cc-small cc-dimtext">not captured — never reconstructed (found ${esc(day(r.detected_with_session))})</span></div>`;
      const chips = r.observations.map((o) => { const [t, k] = decisionLabel(o); return `<span class="fj-chip"><b>${esc(o.symbol)}</b> — ${tag(t, k)}</span>`; }).join("");
      return `<div class="fj-row${detail && detail.session.session_date === r.session_date ? " sl-on" : ""}"><button class="cc-btn cc-mini cc-link fj-date" data-fj-open="${esc(r.session_date)}">${esc(day(r.session_date))}</button>
        ${r.context_timing === "POST_CLOSE_FORWARD_CONTEXT" ? tag("POST-CLOSE", "warn") : ""}<span class="fj-chips">${chips}</span>
        ${r.fills.length ? `<span class="cc-small cc-dimtext">${r.fills.map((f) => `${esc(f.symbol)} ${f.fill_type === "ENTRY" ? "reference entry" : "reference exit"} ${f.status === "FILLED" ? esc(money(f.reference_open_price)) : "unfilled"}`).join(" · ")}</span>` : ""}</div>`;
    }).join("");
    part("history", `<div class="fj-history"><h3>HISTORY <span class="cc-small cc-dimtext">newest first · captured and missed sessions</span></h3>${rows}</div>`);
  }

  // ---- details (stored session; cached) ------------------------------------------------------------------------------
  function drawDetail() {
    if (!detail) { part("detail", ""); return; }
    const s = detail.session, j = detail.journal;
    const obs = detail.observations.filter((o) => !detailSym || o.symbol === detailSym);
    const ds = (s.data && s.data.datasets) || {};
    const feat = (o) => `<div class="fj-tablewrap"><table class="data-table fj-feat"><tr><th>Feature</th><th>Value</th><th>Availability</th><th>Source time</th><th>Timing</th></tr>
      ${o.feature_snapshot.features.map((f) => `<tr><td>${esc(f.name)} <span class="cc-dimtext cc-small">${esc(f.feature_id)}</span></td><td>${esc(fval(f.value, f.unit, f.value_label))}</td>
        <td class="cc-small">${f.availability === "AVAILABLE" ? "available" : `${esc(words(f.availability).toLowerCase())}${f.reason ? ` — ${esc(words(f.reason).toLowerCase())}` : ""}`}</td>
        <td class="cc-small">${esc(f.timing === "AT_CLOSE" ? whenNY(f.source_timestamp) : when(f.source_timestamp))}</td><td class="cc-small">${esc(words(f.timing || "").toLowerCase())}</td></tr>`).join("")}</table></div>`;
    const prov = (o) => {
      const out = [];
      if (o.research) out.push(o.research.source === "SAVED_SNAPSHOT" ? `Research: saved snapshot #${esc(o.research.snapshot_id)} from ${esc(when(o.research.created_at))} (${o.research.existed_at_close ? "existed at the close" : "saved after the close"}) · ${esc(o.research.research_view)} · ${esc(o.research.age_hours_at_capture)} h old at capture` : `Research: ${esc(o.research.message || "unavailable")}`);
      const ev = o.event || {};
      if (ev.stock) out.push(`Events (${esc(o.symbol)}): ${esc(words(ev.stock.status))}${ev.stock.computed_level ? ` · computed level ${esc(ev.stock.computed_level)}` : ""}${ev.stock.note ? ` — ${esc(ev.stock.note)}` : ""}`);
      if (ev.market) out.push(`Macro events: ${esc(words(ev.market.status))} · read ${esc(when(ev.market.captured_at))}`);
      return out.length ? `<div class="cc-small">${out.map((x) => `<div>${x}</div>`).join("")}</div>` : "";
    };
    const block = (o) => {
      const [t, k] = decisionLabel(o);
      const lc = o.lifecycle || {};
      const levels = lc.levels_used ? `<div class="cc-small cc-dimtext">Entry-time levels this session: invalidation ${esc(money(lc.levels_used.invalidation_level))} · target ${esc(money(lc.levels_used.target_level))}${isNum(lc.levels_used.level_factor) && Math.abs(lc.levels_used.level_factor - 1) > 1e-9 ? ` · rebased ×${esc(lc.levels_used.level_factor.toFixed(6))} (split / dividend adjustment)` : ""}</div>` : "";
      return `<div class="fj-dsym"><div class="fj-card-head"><b>${esc(o.symbol)}</b>${tag(t, k)}<span class="cc-small cc-dimtext">${esc(REASON[o.reason_code] || words(o.reason_code))}</span></div>
        ${o.bar ? `<div class="cc-small cc-dimtext">Session bar: open ${esc(money(o.bar.open))} · high ${esc(money(o.bar.high))} · low ${esc(money(o.bar.low))} · close ${esc(money(o.bar.close))}</div>` : ""}
        ${o.evaluation_trace ? `<div class="cc-label">${o.evaluated_side === "EXIT" ? "EXIT RULE TRACE" : "ENTRY CONDITION TRACE"} — ${esc(o.rules_met)} / ${esc(o.rules_total)}</div><ul class="fj-conds">${o.evaluated_side === "EXIT" ? exitRows(o.evaluation_trace) : leafRows(o.evaluation_trace.trace, 0)}</ul>` : ""}
        ${levels}${refBlock(o, detail)}${prov(o)}
        <details${detailSym ? " open" : ""}><summary class="cc-small">Feature snapshot (${esc(o.feature_snapshot.features.length)} features)</summary>${feat(o)}</details></div>`;
    };
    part("detail", `<div class="fj-detail"><div class="cc-head"><h3>SESSION DETAILS — ${esc(day(s.session_date).toUpperCase())}${detailSym ? ` · ${esc(detailSym)}` : ""}</h3>
        <div class="cc-head-tools">${detailSym ? `<button class="cc-btn cc-mini" data-fj="allsyms">All symbols</button>` : ""}<button class="cc-btn cc-mini cc-link" data-fj="closedetail">Close</button></div></div>
      <div class="fj-meta">
        <div><span class="cc-label">STRATEGY VERSION</span><div>${esc(j.name)} · v${esc(j.version_number)}</div></div>
        <div><span class="cc-label">SPEC HASH</span><div><code>${esc(short(s.spec_hash || j.spec_hash))}</code></div></div>
        <div><span class="cc-label">SESSION</span><div>${esc(s.session_date)} · ${esc(words(s.kind))}</div></div>
        <div><span class="cc-label">CAPTURED</span><div>${esc(when(s.recorded_at))}</div></div>
        <div><span class="cc-label">MARKET CLOSE</span><div>${esc(whenNY(s.market_close_snapshot_time))}</div></div>
        <div><span class="cc-label">CONTEXT TIMING</span><div>${s.context_timing ? (s.context_timing === "STRICT_FORWARD" ? tag("STRICT FORWARD", "ok") : tag("POST-CLOSE FORWARD CONTEXT", "warn")) : "—"}</div></div>
        <div><span class="cc-label">CONTINUITY</span><div>${esc(view ? words(view.summary.continuity) : "")}</div></div>
        <div><span class="cc-label">DATA HASH</span><div><code>${esc(short(s.data_hash))}</code> <span class="cc-small cc-dimtext">${esc(Object.keys(ds).length)} dataset(s)</span></div></div></div>
      ${s.kind === "MISSED" ? `<div class="cc-banner cc-warn">This eligible session was not captured. It is recorded as MISSED and was never reconstructed.</div>` : ""}
      ${detail.timing_text ? `<div class="cc-small cc-dimtext">${esc(detail.timing_text)}</div>` : ""}
      ${(s.warnings || []).length ? `<ul class="fj-warns">${s.warnings.map((w) => `<li><b>${esc(words(w.code))}</b><div class="cc-small">${esc(w.text)}</div></li>`).join("")}</ul>` : ""}
      ${obs.map(block).join("")}
      <div class="cc-small cc-dimtext">${esc(detail.reference_fill_text)} Stored observation — opening it never recalculates anything.</div></div>`);
    const el = root.querySelector(".fj-detail");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  function primary() { return list && list.journals.find((j) => j.status !== "ARCHIVED"); }
  function drawArchived() {
    const arch = list ? list.journals.filter((j) => j.status === "ARCHIVED" && (!view || j.journal_id !== view.journal.journal_id)) : [];
    const back = view && view.journal.status === "ARCHIVED" && primary();
    part("archived", arch.length || back ? `<div class="fj-archived"><h3>OTHER JOURNALS FOR THIS VERSION</h3>
      ${back ? `<div class="fj-row"><button class="cc-btn cc-mini" data-fj-journal="${esc(back.journal_id)}">Back to the current journal</button></div>` : ""}
      ${arch.map((j) => `<div class="fj-row"><span>${tag("ARCHIVED", "dim")} started ${esc(when(j.created_at))} · ${esc(j.sessions_captured)} captured · ${esc(j.sessions_missed)} missed</span>
        <button class="cc-btn cc-mini cc-link" data-fj-journal="${esc(j.journal_id)}">View</button></div>`).join("")}</div>` : "");
  }

  // ---- wiring (per part) -------------------------------------------------------------------------------------------
  function wire(el) {
    el.querySelectorAll("[data-fj]").forEach((b) => b.addEventListener("click", () => act(b.dataset.fj)));
    el.querySelectorAll("[data-fj-open]").forEach((b) => b.addEventListener("click", () => openSession(b.dataset.fjOpen, b.dataset.fjSym || "")));
    el.querySelectorAll("[data-fj-journal]").forEach((b) => b.addEventListener("click", () => loadJournal(b.dataset.fjJournal)));
  }
  async function act(a) {
    if (a === "askstart") { confirmStart = true; drawStatus(); }
    else if (a === "cancelstart") { confirmStart = false; drawStatus(); }
    else if (a === "start" || a === "new") await start();
    else if (a === "check") await check();
    else if (a === "record") await record();
    else if (a === "askarchive") { confirmArchive = true; drawAction(); }
    else if (a === "cancelarchive") { confirmArchive = false; drawAction(); }
    else if (a === "archive") await archive();
    else if (a === "closedetail") { detail = null; drawDetail(); drawHistory(); }
    else if (a === "allsyms") { detailSym = ""; drawDetail(); }
  }
  async function start() {
    busy = "starting"; notice = ""; drawStatus();
    const r = await send("/api/forward-tests", { strategy_id: sel.strategy_id, version_number: sel.version_number });
    busy = ""; confirmStart = false;
    if (r.status !== 201) { notice = (r.body && r.body.message) || "The journal could not be started."; drawStatus(); drawAction(); return; }
    view = r.body; pf = null;
    await loadList();
    drawAll();
  }
  async function check() {
    busy = "checking"; notice = ""; drawAction();
    const r = await send(`/api/forward-tests/${encodeURIComponent(view.journal.journal_id)}/preflight`);
    busy = "";
    if (r.status !== 200) { notice = (r.body && r.body.message) || "The check failed."; pf = null; }
    else pf = r.body;
    drawAction();
  }
  async function record() {
    busy = "recording"; notice = ""; drawAction();
    const id = view.journal.journal_id;
    const r = await send(`/api/forward-tests/${encodeURIComponent(id)}/record`);
    busy = "";
    if (r.status !== 200) { notice = (r.body && r.body.message) || "Nothing was recorded."; drawAction(); return; }
    const sess = r.body.session;
    cache.set(`${id}|${sess.session.session_date}`, sess);
    view = { ...r.body.journal, latest_session: r.body.status === "RECORDED" ? sess : view.latest_session };
    pf = null;
    notice = r.body.status === "ALREADY_RECORDED" ? r.body.message : "";
    drawStatus(); drawCycles(); drawAction(); drawLatest(); drawHistory();
    if (r.body.status === "ALREADY_RECORDED") { detail = sess; detailSym = ""; drawDetail(); }
    else { const el = root.querySelector(".fj-latest"); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" }); }
  }
  async function archive() {
    busy = "archiving";
    const r = await send(`/api/forward-tests/${encodeURIComponent(view.journal.journal_id)}/archive`);
    busy = ""; confirmArchive = false;
    if (r.status !== 200) { notice = (r.body && r.body.message) || "The journal could not be archived."; drawAction(); return; }
    view = r.body; pf = null;
    await loadList();
    drawAll();
  }
  async function openSession(date, sym) {
    const id = view.journal.journal_id, key = `${id}|${date}`;
    if (!cache.has(key)) {
      const r = await getJSON(`/api/forward-tests/${encodeURIComponent(id)}/sessions/${encodeURIComponent(date)}`);
      if (r.status !== 200) { notice = "Could not open that session."; drawAction(); return; }
      cache.set(key, r.body);
    }
    detail = cache.get(key); detailSym = sym;
    drawDetail(); drawHistory();
  }
  async function loadList() {
    const r = await getJSON(`/api/forward-tests?strategy_id=${encodeURIComponent(sel.strategy_id)}&version_number=${encodeURIComponent(sel.version_number)}`);
    list = r.status === 200 ? r.body : { journals: [], eligibility: null };
  }
  async function loadJournal(id) {
    const r = await getJSON(`/api/forward-tests/${encodeURIComponent(id)}`);
    if (r.status !== 200) { notice = "Could not open that journal."; drawAction(); return; }
    view = r.body; pf = null; detail = null; notice = "";
    if (view.latest_session) cache.set(`${id}|${view.latest_session.session.session_date}`, view.latest_session);
    drawAll();
  }

  // ---- entry points used by the Strategy Lab (strategy_lab.js) ---------------------------------------------------------
  async function show(v) {
    sel = v; view = null; list = null; pf = null; detail = null; notice = ""; confirmStart = false; confirmArchive = false;
    shell();
    part("status", `<div class="cc-small cc-dimtext">Loading…</div>`);
    await loadList();
    const p = primary();
    if (p) await loadJournal(p.journal_id); else drawAll();
  }
  async function select(v) {
    // Opening another version while the panel is open reloads it for that version; nothing is ever evaluated here.
    if (!v) { sel = null; open = false; root.innerHTML = ""; return; }
    if (open && (!sel || v.strategy_id !== sel.strategy_id || v.version_number !== sel.version_number)) await show(v);
    else sel = v;
  }
  async function openPanel(v) {
    if (!v) return;
    open = true;
    await show(v);
    const el = root.querySelector(".fj");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" });
  }
  window.ForwardJournal = { select, open: openPanel, get state() { return { sel, open, list, view, pf, busy, notice, detail: detail && detail.session.session_date, cached: cache.size }; } };
})();
