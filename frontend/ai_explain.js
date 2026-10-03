// ai_explain.js — Stage 3.8 AI EXPLANATION of an already-computed Strategy Fit result or Evidence view.
//
// Nothing here runs by itself: an explanation starts only when the user clicks "Explain this setup" / "Explain this
// evidence", sees the expected Claude calls (1, or 0 when cached or when a local explanation is enough), and confirms.
// The request only NAMES the result (strategy version, symbol, session, evaluation time — or version / run / journal);
// the server reads the authoritative result itself. Output is rendered as escaped text only. Each explanation belongs
// to one exact view (a slot key): a late answer can never appear under another stock, run or journal.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "");
  const S = new Map();           // slot key -> { kind, body, family, phase, preview, result, error, ctrl, seq }
  const FAMILY_LAST = new Map(); // "fit|vid|SYM" or "ev|vid|run|journal" -> slot key that has an explanation
  let calls = 0;                 // Claude calls made from this page (for validation)
  const GIVE_UP_MS = 90000;      // the server allows one 60 s model request; never leave the button waiting forever

  function slot(kind, key, family, body) {
    let st = S.get(key);
    if (!st) { st = { kind, body, family, phase: "idle", seq: 0 }; S.set(key, st); }
    st.body = body;
    return `<div class="ax-slot" data-ax-slot="${esc(key)}">${render(key)}</div>`;
  }
  function fitSlot(s, cur) {
    const body = { strategy_version_id: s.strategy_version_id, symbol: cur.symbol, decision_session: cur.decision_session || "", evaluated_at: cur.evaluated_at };
    if (!body.decision_session) return "";
    return slot("fit", `fit|${s.strategy_version_id}|${cur.symbol}|${body.decision_session}|${cur.evaluated_at}`, `fit|${s.strategy_version_id}|${cur.symbol}`, body);
  }
  function evidenceSlot(d) {
    const body = { strategy_version_id: d.identity.strategy_version_id, backtest_run_id: d.selection.backtest_run_id || null,
      forward_journal_id: d.selection.forward_journal_id || null };
    const fam = `ev|${body.strategy_version_id}|${body.backtest_run_id || ""}|${body.forward_journal_id || ""}`;
    return slot("evidence", `${fam}|${d._loadedAt || ""}`, fam, body);
  }
  const url = (kind, preview) => `/api/ai-explain/${kind === "fit" ? "strategy-fit" : "evidence"}${preview ? "/preview" : ""}`;
  const LABEL = { fit: "Explain this setup", evidence: "Explain this evidence" };
  const BASIS = { fit: "the current deterministic rule result and stored evidence summaries", evidence: "the stored historical and forward evidence of this exact version" };

  function list(title, items, mark) {
    return items && items.length ? `<div class="ax-sec"><span class="cc-label">${esc(title)}</span><ul>${items.map((x) => `<li>${mark ? `<span class="ax-mark">${mark}</span> ` : ""}${esc(x)}</li>`).join("")}</ul></div>` : "";
  }
  // the explanation body (shared with the Stage 3.9 history view, which shows a SAVED explanation exactly as it was)
  function sections(kind, e, local) {                // local: a not-evaluated version — plain facts, never a ✓ / ✕ rule result
    e = e || {};
    return `<p class="ax-summary">${esc(e.summary)}</p>` + (kind === "fit"
      ? (local ? list("What is true now", e.what_is_true_now) : list("What the rules say — met", e.what_is_true_now, "✓"))
        + list("What the rules say — not met or unavailable", e.what_is_not_met, "✕")
        + list("Historical / forward context", e.evidence_context) + list("Limitations", e.limitations)
      : list("Historical", e.historical) + list("Forward", e.forward) + list("What can and cannot be compared", e.differences) + list("Limitations", e.limitations));
  }
  function panel(st) {
    const r = st.result;
    const source = r.status === "LOCAL" ? "No AI call needed" : r.cache_hit ? "Cached" : "Generated now";
    const h = r.history || {};
    const saved = h.enabled && h.history_id ? " · Saved to history" : "";
    return `<div class="ax-panel"><div class="ax-head"><span class="cc-label">AI EXPLANATION</span>
        <span class="cc-tag cc-dim">${esc(r.grounded_in && r.grounded_in.basis)}</span><span class="cc-small cc-dimtext">${esc(source)}</span>
        <button type="button" class="cc-btn cc-mini cc-link" data-ax-act="close" data-ax-key="${esc(st.key)}">Hide</button></div>
      ${sections(st.kind, r.explanation, r.status === "LOCAL")}
      <div class="ax-foot cc-small cc-dimtext">Grounded in: ${esc(r.grounded_in && r.grounded_in.label)} · Claude calls: <b>${esc(r.claude_calls)}</b> · ${esc(source)}${esc(saved)}</div>
      <details class="ax-tech"><summary class="cc-small">Technical details</summary><div class="cc-small cc-dimtext">
        Input fingerprint <code>${esc(String(r.input_fingerprint || "").slice(0, 16))}</code> · prompt ${esc(r.prompt_version)} ·
        ${esc(r.provider)} / ${esc(r.model)} · ${r.cache_hit ? "cache hit" : "no cache hit"} · Claude calls ${esc(r.claude_calls)} · ${esc(whenNY(r.generated_at))}
        <div class="ax-hist">History saving ${h.enabled ? "ON" : "OFF"}${h.error ? ` · ${esc(h.error)}` : ""} ·
          <button type="button" class="cc-btn cc-mini cc-link" data-axh-open="${esc(h.history_id || "")}">AI explanation history</button></div></div></details></div>`;
  }
  function render(key) {
    const st = S.get(key);
    if (!st) return "";
    st.key = key;
    const btn = `<button type="button" class="cc-btn cc-mini" data-ax-act="preview" data-ax-key="${esc(key)}">${esc(LABEL[st.kind])}</button>`;
    const prior = FAMILY_LAST.get(st.family);
    const priorNote = prior && prior !== key && st.phase === "idle"
      ? `<div class="cc-small cc-dimtext">An explanation exists for a previous view of this result; it is hidden here. Explaining again costs 0 calls if nothing changed.</div>` : "";
    if (st.phase === "idle") return `<div class="ax-row">${btn}</div>${priorNote}`;
    if (st.phase === "previewing") return `<div class="ax-row cc-small cc-dimtext">Checking what an explanation would need… (no Claude call)</div>`;
    if (st.phase === "confirm") {
      const p = st.preview;
      const n = p.expected_claude_calls;
      return `<div class="ax-confirm"><span class="cc-label">AI EXPLANATION</span>
        <div class="cc-small">This explanation will use: <b>${esc(n)} Claude call${n === 1 ? "" : "s"}</b>${p.cache_hit ? (p.cached_usable === false ? " — the cached answer was withheld" : " — cached explanation available") : ""}</div>
        <div class="cc-small">It will read: ${esc(BASIS[st.kind])}.</div>
        ${p.budget ? `<div class="cc-small cc-dimtext">Explanation budget: ${esc(p.budget.used_today)} / ${esc(p.budget.daily_limit)} used today · ${esc(p.budget.used_last_hour)} / ${esc(p.budget.hourly_limit)} this hour (separate from Research)</div>` : ""}
        <div class="cc-small">It will NOT: place orders, change your strategy, or rank strategies.</div>
        ${p.history && p.history.enabled ? `<div class="cc-small cc-dimtext">History saving is ON: an accepted explanation is saved locally (append-only).</div>` : ""}
        ${p.available && p.cached_usable !== false ? "" : `<div class="cc-small ax-warn">${esc(p.message)}</div>`}
        <div class="ax-row"><button type="button" class="cc-btn cc-mini" data-ax-act="cancel" data-ax-key="${esc(key)}">Cancel</button>
          <button type="button" class="cc-btn cc-mini cc-primary" data-ax-act="explain" data-ax-key="${esc(key)}"${p.available ? "" : " disabled"}>Explain</button></div></div>`;
    }
    if (st.phase === "loading") return `<div class="ax-row cc-small">Explaining… (${esc(st.preview ? st.preview.expected_claude_calls : 1)} Claude call) <button type="button" class="cc-btn cc-mini cc-link" data-ax-act="abort" data-ax-key="${esc(key)}">Cancel</button></div>`;
    if (st.phase === "error") return `<div class="ax-error cc-small"><b>Explanation unavailable.</b> ${esc(st.error)} The deterministic result above is unchanged.</div><div class="ax-row">${btn}</div>`;
    if (st.phase === "done") {
      const r = st.result;
      if (r.status === "OK" || r.status === "LOCAL") return panel(st);
      return `<div class="ax-error cc-small"><b>${r.status === "WITHHELD" ? "Explanation withheld." : "Explanation unavailable."}</b> ${esc(r.message)} · Claude calls: ${esc(r.claude_calls)}</div><div class="ax-row">${btn}</div>`;
    }
    return btn;
  }
  function paint(key) { document.querySelectorAll(`[data-ax-slot="${CSS.escape(key)}"]`).forEach((el) => { el.innerHTML = render(key); }); }

  async function post(st, key, preview, extra) {
    if (st.ctrl) st.ctrl.abort();
    st.ctrl = new AbortController();
    const signal = AbortSignal.any && AbortSignal.timeout ? AbortSignal.any([st.ctrl.signal, AbortSignal.timeout(GIVE_UP_MS)]) : st.ctrl.signal;
    const my = ++st.seq;
    let r, body;
    try {
      r = await fetch(url(st.kind, preview), { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...st.body, ...(extra || {}) }), signal });
      body = await r.json().catch(() => ({}));
    } catch (e) {
      if (my !== st.seq) return null;                         // replaced or cancelled
      return { ok: false, message: e && e.name === "TimeoutError" ? "No answer within 90 seconds, so the request was stopped."
        : e && e.name === "AbortError" ? "Cancelled." : "The server did not answer." };
    }
    if (my !== st.seq) return null;                           // a newer action owns this slot
    return r.status === 200 ? { ok: true, body } : { ok: false, message: body.message || "The explanation could not be prepared." };
  }
  async function act(a, key) {
    const st = S.get(key);
    if (!st) return;
    if (a === "cancel" || a === "close") { st.phase = "idle"; paint(key); return; }
    if (a === "abort") { if (st.ctrl) st.ctrl.abort(); st.seq++; st.phase = "idle"; paint(key); return; }
    if (a === "preview") {
      st.phase = "previewing"; paint(key);
      const res = await post(st, key, true);
      if (!res) return;
      if (!res.ok) { st.phase = "error"; st.error = res.message; paint(key); return; }
      if (res.body.mode === "LOCAL") {                         // no AI call needed: show it at once
        st.result = { ...res.body, status: "LOCAL", claude_calls: 0, cache_hit: false, generated_at: new Date().toISOString() };
        st.phase = "done"; FAMILY_LAST.set(st.family, key); paint(key); return;
      }
      st.preview = res.body; st.phase = "confirm"; paint(key); return;
    }
    if (a === "explain" && st.phase === "confirm") {
      st.phase = "loading"; paint(key);
      const res = await post(st, key, false, { input_fingerprint: st.preview.input_fingerprint });
      if (!res) return;
      if (!res.ok) { st.phase = "error"; st.error = res.message; paint(key); return; }
      calls += res.body.claude_calls || 0;
      if (res.body.claude_calls && typeof window.refreshAIUsage === "function") window.refreshAIUsage();   // header budgets
      st.result = res.body; st.phase = "done";
      if (res.body.status === "OK") FAMILY_LAST.set(st.family, key);
      paint(key);
    }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-ax-act]");
    if (b && !b.disabled) act(b.dataset.axAct, b.dataset.axKey);
  });
  window.AIExplain = { fitSlot, evidenceSlot, sections, get state() { return { slots: S.size, calls, phases: [...S.values()].map((x) => x.phase) }; } };
})();
