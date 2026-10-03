// ai_history.js — Stage 3.9 AI EXPLANATION HISTORY (optional, local, append-only; OFF by default).
//
// A small Strategy Lab workspace. Nothing here calls Claude, regenerates text or touches the AI cache: it lists what
// the app already showed (newest first, 50 at a time) and opens one saved explanation exactly as it was stored. The
// only write is the explicit "Enable history" / "Turn off" setting. Saved explanations are never edited or deleted.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("axh-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "");
  const dayNY = (v) => (v ? new Date(v).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "America/New_York" }) : "");
  const FILTERS = [["all", "All"], ["fit", "Strategy Fit"], ["evidence", "Evidence"], ["local", "Local"], ["claude", "Claude"]];
  const KIND = { STRATEGY_FIT_EXPLANATION: "Strategy Fit", EVIDENCE_EXPLANATION: "Evidence" };
  const ORIGIN = { generated: "Claude · generated", cached: "Claude · shown from cache", local: "Local · no AI call" };
  let enabled = null, filter = "all", items = [], next = null, busy = false, error = null, detail = null, seq = 0, requests = 0;

  async function getJSON(url, opts) {
    requests++;
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.message || `HTTP ${r.status}`);
    return body;
  }
  const query = () => (filter === "fit" || filter === "evidence" ? `kind=${filter}` : filter === "local" || filter === "claude" ? `origin=${filter}` : "kind=all");

  async function load(more) {
    const my = ++seq;
    busy = true; error = null; draw();
    try {
      const b = await getJSON(`/api/explanation-history?${query()}&limit=50${more && next ? `&before=${encodeURIComponent(next)}` : ""}`);
      if (my !== seq) return;                                   // a newer filter owns the list
      items = more ? items.concat(b.items) : b.items; next = b.next; enabled = b.enabled;
    } catch (e) { if (my === seq) error = e.message; }
    if (my === seq) { busy = false; draw(); }
  }
  async function toggle() {
    busy = true; draw();
    try { enabled = (await getJSON("/api/explanation-history/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !enabled }) })).enabled; }
    catch (e) { error = e.message; }
    busy = false; draw();
  }
  async function openItem(id) {
    if (!id) return;
    const my = ++seq;
    busy = true; draw();
    try { const d = await getJSON(`/api/explanation-history/${encodeURIComponent(id)}`); if (my === seq) detail = d; }
    catch (e) { if (my === seq) error = e.message; }
    if (my === seq) { busy = false; draw(); }
  }

  function row(x) {
    const what = [dayNY(x.saved_at), KIND[x.explanation_type], x.symbol, x.label].filter(Boolean).join(" · ");
    const status = x.fit_status || (x.continuity ? `forward continuity ${x.continuity}` : "");
    return `<li><button type="button" class="axh-item" data-axh-act="open" data-id="${esc(x.history_id)}">
      <span class="axh-what">${esc(what)}</span>
      <span class="cc-small">${esc(status)}</span>
      <span class="cc-small cc-dimtext">${esc(ORIGIN[x.origin] || x.origin)}</span></button></li>`;
  }
  function facts(g) {
    const kv = (k, v) => (v == null || v === "" ? "" : `<div><span class="cc-label">${esc(k)}</span> ${esc(v)}</div>`);
    if (g.fit_status !== undefined) {
      return kv("Stock", g.symbol) + kv("Decision session", g.decision_session) + kv("Rule result", g.fit_status)
        + kv("Conditions met", g.conditions && g.conditions.met) + kv("Context timing", g.context_timing && String(g.context_timing).replace(/_/g, " ").toLowerCase())
        + ((g.condition_trace || []).length ? `<ul class="axh-trace">${g.condition_trace.map((c) => `<li>${esc(c.condition)} — ${esc(c.result)}${c.observed != null ? ` (observed ${esc(c.observed)})` : ""}</li>`).join("")}</ul>` : "");
    }
    const h = g.historical || {}, f = g.forward || {};
    return kv("Historical", [h.run, h.selection, h.period, h.closed_trades != null ? `${h.closed_trades} closed trades` : h.status].filter(Boolean).join(" · "))
      + kv("Forward", [f.journal_status, f.continuity, f.completed_reference_cycles != null ? `${f.completed_reference_cycles} completed cycles` : f.status].filter(Boolean).join(" · "))
      + kv("MFE / MAE", g.mfe_mae_tracking)
      + ((g.compatible_differences || []).length ? `<ul class="axh-trace">${g.compatible_differences.map((d) => `<li>${esc(d.measure)}: ${esc(d.difference)}</li>`).join("")}</ul>` : "");
  }
  function detailView(d) {
    const kind = d.explanation_type === "STRATEGY_FIT_EXPLANATION" ? "fit" : "evidence";
    const body = window.AIExplain && window.AIExplain.sections ? window.AIExplain.sections(kind, d.explanation, d.origin === "local") : "";
    const g = d.grounding_summary || {};
    return `<div class="ax-panel axh-detail">
      <div class="ax-head"><span class="cc-label">SAVED AI EXPLANATION</span><span class="cc-tag cc-dim">${esc(KIND[d.explanation_type])}</span>
        <span class="cc-small cc-dimtext">${esc(ORIGIN[d.origin] || d.origin)}</span>
        <button type="button" class="cc-btn cc-mini cc-link" data-axh-act="back">Back to list</button></div>
      <div class="cc-small cc-dimtext">Stored exactly as it was shown on ${esc(whenNY(d.generated_at))} — not regenerated (0 Claude calls). The deterministic view may have changed since.</div>
      ${body}
      <details class="ax-tech" open><summary class="cc-small">Grounded in</summary><div class="axh-facts cc-small">${esc(g.grounded_in || g.label)}${facts(g)}</div></details>
      <details class="ax-tech"><summary class="cc-small">Technical details</summary><div class="cc-small cc-dimtext">
        Input fingerprint <code>${esc(String(d.input_fingerprint || "").slice(0, 16))}</code> · prompt ${esc(d.prompt_version)} · ${esc(d.provider)} / ${esc(d.model)} ·
        Claude calls ${esc(d.claude_calls)} · generated ${esc(whenNY(d.generated_at))} · saved ${esc(whenNY(d.saved_at))} · history id <code>${esc(d.history_id.slice(0, 12))}</code></div></details></div>`;
  }
  function draw() {
    const on = enabled === true;
    const head = `<section class="cc-card axh">
      <div class="cc-head"><h2>AI EXPLANATION HISTORY <span class="cc-small cc-dimtext">local · append-only · never regenerated</span></h2>
        <span class="axh-state cc-small">${enabled === null ? "" : on ? "History saving <b>ON</b>" : "History saving <b>OFF</b>"}
          ${enabled === null ? "" : `<button type="button" class="cc-btn cc-mini${on ? "" : " cc-primary"}" data-axh-act="toggle"${busy ? " disabled" : ""}>${on ? "Turn off" : "Enable history"}</button>`}</span></div>
      <p class="cc-small cc-dimtext">${on
        ? "Accepted explanations (Claude or local) are saved with the view they explained. Showing the same explanation again adds nothing; saved ones are never changed or deleted."
        : "Off by default: explanations are kept in memory only. Turn history on to keep a local record of what the app explained and which deterministic view it explained."}
        History is not evidence — no calculation reads it.</p>`;
    if (detail) { root.innerHTML = `${head}${detailView(detail)}</section>`; return; }
    const chips = `<div class="sf-filters axh-filters" role="group" aria-label="History filter">${FILTERS.map(([k, t]) =>
      `<button type="button" data-axh-act="filter" data-f="${k}" class="${filter === k ? "active" : ""}" aria-pressed="${filter === k}">${esc(t)}</button>`).join("")}</div>`;
    const list = items.length ? `<ul class="axh-list">${items.map(row).join("")}</ul>`
      : `<p class="cc-small cc-dimtext">${busy ? "Loading…" : on ? "Nothing saved yet for this filter." : "No saved explanations."}</p>`;
    root.innerHTML = `${head}${chips}${error ? `<div class="ax-error cc-small">${esc(error)}</div>` : ""}${list}
      ${next ? `<button type="button" class="cc-btn cc-mini" data-axh-act="more"${busy ? " disabled" : ""}>Load more</button>` : ""}</section>`;
  }

  root.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-axh-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.axhAct;
    if (a === "toggle") toggle();
    else if (a === "filter") { filter = b.dataset.f; detail = null; next = null; load(false); }
    else if (a === "more") load(true);
    else if (a === "open") openItem(b.dataset.id);
    else if (a === "back") { detail = null; if (items.length) draw(); else load(false); }
  });
  document.addEventListener("click", (e) => {                 // "AI explanation history" inside an explanation's Technical details
    const b = e.target.closest && e.target.closest("[data-axh-open]");
    if (b) open(b.dataset.axhOpen || null);
  });
  function show() { load(false); }
  function open(id) { window.StrategyFit.setView("history"); if (id) openItem(id); }
  window.StrategyFit.addWorkspace({ id: "history", label: "AI history", cls: "axh-mode", show });
  window.AIHistory = { open, get state() { return { enabled, filter, items: items.length, next: !!next, busy, detail: detail && detail.history_id, requests }; } };
})();
