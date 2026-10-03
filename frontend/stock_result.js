// stock_result.js — Stage 2.7G.3: make a card's ANALYZE visible and actionable (presentation + integration only).
//
// Flow: limit/cache check (2.7G planner) → confirm → existing research endpoint (the only step that may call
// Claude) → POST /api/insights/stock-decision (the unchanged 2.7G decision for THIS stock; never calls Claude)
// → result shown under that card. Research and the deterministic state stay separate. Showing the result,
// "Full analysis", its tabs, "Show details", "Collapse" and re-opening all use data already returned — no request,
// no AI call. New-AI-call counts are measured from /api/ai/usage before/after, never assumed.
//
// Stage 2.8C: the result is a desktop research panel — a persistent stock header, the deterministic Current View
// as the visual centre (Research View shown separately), Market → Stock → Portfolio, capped Why lists, a price map
// drawn only from the EXISTING support/resistance, and the full analysis as tabs. Nothing is recalculated here.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "Unavailable" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const signed = (v) => (n(v) == null ? "Unavailable" : `${n(v) >= 0 ? "+" : "−"}${money(Math.abs(n(v)))}`);
  const spct = (v) => (n(v) == null ? "Unavailable" : `${n(v) >= 0 ? "+" : "−"}${Math.abs(n(v)).toFixed(2)}%`);
  const pct = (v) => (n(v) == null ? "Unavailable" : `${n(v).toFixed(2)}%`);
  const move = (v) => (n(v) == null ? "" : n(v) > 0 ? "pct-up" : n(v) < 0 ? "pct-down" : "");
  const val = (v) => (v == null || v === "" ? "Unavailable" : esc(v));
  const cap = (s) => (s ? String(s).charAt(0) + String(s).slice(1).toLowerCase() : "");
  const tag = (text, kind) => `<span class="cc-tag cc-${esc(kind)}">${esc(text)}</span>`;
  const when = (v) => (v ? new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "Unavailable");
  const shortDate = (d) => (d ? new Date(`${String(d).slice(0, 10)}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "");
  const eventName = (t) => { const m = /\(([^)]+)\)\s*$/.exec(t || ""); return m ? m[1] : (t || ""); };
  const sentence = (s) => (s ? String(s).charAt(0) + String(s).slice(1).toLowerCase() : "");

  // Semantic colours, shared with the dashboard: ok = green supportive/improving · warn = yellow mixed/confirmation ·
  // alert = orange caution · bad = red needs attention/weakening · dim = grey unknown/stale/unavailable · info = blue.
  const STATE_KIND = {
    "SUPPORTED — MONITOR": "ok", "SETUP IMPROVING": "ok", "WORTH FURTHER REVIEW": "ok", "MONITOR": "info",
    "WAIT FOR CONFIRMATION": "warn", "MONITOR SUPPORT": "warn", "WAIT FOR PULLBACK": "warn", "WAIT FOR EVENT": "warn",
    "WAIT FOR BREAKOUT CONFIRMATION": "warn", "WAIT FOR MARKET CONFIRMATION": "warn", "MIXED — KEEP WATCHING": "warn",
    "HIGHER-RISK SETUP": "alert",
    "SETUP WEAKENING": "bad", "EVENT RISK — REVIEW": "bad", "PROTECT GAINS / REVIEW RISK": "bad", "REVIEW POSITION SIZE": "bad",
    "RESEARCH NEEDED": "dim", "DATA STALE": "dim", "INSUFFICIENT DATA": "dim",
  };
  const FRESH = { FRESH: "ok", AGING: "warn", STALE: "dim", MISSING: "dim", UNKNOWN: "dim" };
  const LAYER = { Supportive: "ok", OK: "ok", Mixed: "warn", Watch: "warn", Caution: "alert", Unavailable: "dim" };
  const WORD = { Positive: "ok", Normal: "info", Weak: "alert", Strong: "info", Light: "info", High: "bad", Medium: "warn", Low: "ok", Up: "ok", Down: "alert", Mixed: "warn" };
  const FIT = { HIGH: ["Higher attention", "alert"], MEDIUM: ["Watch", "warn"] };
  const LIMIT = { "hourly AI call limit": "Hourly AI limit reached", "daily AI call limit": "Daily AI limit reached" };
  const TABS = [["technical", "Technical"], ["catalysts", "Catalysts"], ["risks", "Risks"], ["market", "Market / sector"],
    ["portfolio", "Portfolio"], ["evidence", "Evidence"], ["ai", "AI summary"]];
  const results = new Map();          // symbol -> { dec, status }  (this tab only)
  let openSym = null;

  const stateKind = (s) => STATE_KIND[s] || "info";
  const stateTag = (s) => `<span class="cc-state cc-${stateKind(s)}">${esc(s)}</span>`;
  const locKind = (w) => (/extended/.test(w || "") ? "alert" : /^Near resistance/.test(w || "") ? "warn" : /^Below/.test(w || "") ? "bad"
    : /^(Near support|Above)/.test(w || "") ? "info" : "dim");
  // research age in plain words: "just now", "12m", "9h" (thresholds stay with the backend's freshness labels)
  // rounded DOWN, so an age never appears to cross the backend's freshness threshold (23.7h -> "23h", not "24h")
  function age(h) {
    if (n(h) == null) return "";
    return h < 0.05 ? "just now" : h < 1 ? `${Math.floor(h * 60)}m` : `${Math.floor(h)}h`;
  }
  const freshTag = (f, h) => `<span class="cc-fresh cc-${FRESH[f] || "dim"}">${f === "MISSING" ? "RESEARCH NEEDED" : esc(f || "UNKNOWN")}${f !== "MISSING" && age(h) ? ` · ${esc(age(h))}` : ""}</span>`;
  const ageWords = (t) => String(t).replace(/(\d+(?:\.\d+)?) hours old/g, (_, h) => (age(Number(h)) === "just now" ? "updated just now" : `${age(Number(h))} old`));
  // 2.8E: one presentation pass for beginner-facing sentences from the backend (wording only — never the meaning):
  // plural sector names used as adjectives, raw hour counts, and "1 days".
  const SECTOR_ADJ = { Semiconductors: "Semiconductor", Financials: "Financial", Industrials: "Industrial", Utilities: "Utility" };
  const tidy = (t) => ageWords(String(t == null ? "" : t))
    .replace(/\b(Semiconductors|Financials|Industrials|Utilities)\b(?= (?:stocks|exposure|holdings|sector|trend|movement|concentration)\b)/gi,
      (w) => { const k = Object.keys(SECTOR_ADJ).find((x) => x.toLowerCase() === w.toLowerCase()); const r = SECTOR_ADJ[k]; return w[0] === w[0].toLowerCase() ? r.toLowerCase() : r; })
    .replace(/\((\d+(?:\.\d+)?) h\)/g, (_, h) => `(${age(Number(h))})`)
    .replace(/\b1(\.0)? (trading )?days\b/g, "1 $2day");
  const days = (v) => (n(v) == null ? "" : n(v) === 1 ? "1 day" : `${v} days`);
  // "today" only when the market data is from today (the dashboard's session label); otherwise "last session"
  const sessionWord = () => { const cc = window.CommandCenter && window.CommandCenter.last; return cc && cc.session && cc.session.is_today === false ? "last session" : "today"; };
  const list = (a, cls, empty) => (a.length ? `<ul class="cc-list sa-list">${a.map((t) => `<li class="${cls}">${esc(tidy(t))}</li>`).join("")}</ul>`
    : `<p class="cc-small cc-dimtext">${esc(empty || "Nothing specific.")}</p>`);

  const getJSON = (u, o) => fetch(u, o).then((r) => r.json());
  const post = (u, b) => getJSON(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  const aiCallsToday = () => getJSON("/api/ai/usage").then((u) => u.calls).catch(() => null);
  // The research endpoint answers 200 even when the AI parts were gated; its gating messages start with "AI analysis".
  const aiUnavailable = (d) => [d && d.narrative && d.narrative.whats_happening, d && d.catalyst_note, d && d.risk_explanation]
    .find((t) => /^AI analysis/.test(t || "")) || null;

  function wrapperOf(host) { return host.closest(".cc-stock, .cc-watch-row"); }
  // The header's Analyze button re-uses the card's own Analyze button (same capacity check + confirmation).
  function rerun(host) {
    const w = wrapperOf(host);
    const b = w && w.querySelector(":scope > .cc-actions [data-analyze], :scope > .cc-watch-item [data-analyze-w]");
    if (b) b.click();
  }

  function collapse(sym) {
    document.querySelectorAll(`[data-result-for="${sym}"]`).forEach((h) => {
      h.innerHTML = "";
      const w = wrapperOf(h);
      if (w) { w.classList.remove("cc-expanded"); if (w.dataset.title) { w.title = w.dataset.title; delete w.dataset.title; } }
    });
    if (openSym === sym) openSym = null;
  }

  // ---- persistent stock header: identity · CURRENT VIEW (primary) · research view (separate) · position -----------
  function researchLine(r, failed) {
    const label = `<div class="cc-label">${failed ? "EXISTING SAVED RESEARCH" : "RESEARCH VIEW"}</div>`;
    if (!r || !r.available) return `${label}<div class="sa-hv">${failed ? "None available" : freshTag("MISSING")}</div>`;
    return `${label}<div class="sa-hv">${tag(cap(r.research_view), "info")} ${freshTag(r.freshness, r.age_hours)}</div>
      <div class="sa-sub">Separate from the current view${r.data_quality ? ` · quality ${esc(cap(r.data_quality))}` : ""}</div>`;
  }

  function headHtml(sym, dec, failed) {
    const c = dec && dec.decision, r = dec && dec.research, p = c && c.position, owned = dec ? dec.owned : null;
    const needAnalyze = !r || !r.available || r.freshness === "STALE" || r.freshness === "MISSING";
    const where = owned === true ? tag("You own this", "info") : owned === false ? tag("Not owned", "dim") : tag("Ownership unknown", "dim");
    const pos = owned === true && p ? `<div class="sa-hv">${money(p.value)}</div><div class="sa-sub">Open P&amp;L <span class="${move(p.open_pnl)}">${signed(p.open_pnl)} (${spct(p.open_pnl_pct)})</span></div>`
      : `<div class="sa-hv sa-muted">${owned === false ? "Not currently owned" : "Unknown"}</div><div class="sa-sub">${owned === false ? "Watchlist stock" : "Robinhood is not connected"}</div>`;
    return `<div class="sa-head">
      <div class="sa-id"><div><span class="sa-sym">${esc(sym)}</span> ${where}</div>
        <div class="sa-px">${c ? money(c.price) : ""} <span class="${move(c && c.pct_today)}">${c ? spct(c.pct_today) : ""}</span><span class="sa-sub"> ${esc(sessionWord())}</span></div></div>
      <div class="sa-view sa-k-${c ? stateKind(c.state) : "dim"}"><div class="cc-label">CURRENT VIEW · TODAY'S RULES</div>
        <div class="sa-state">${c ? esc(c.state) : "UNAVAILABLE"}</div>${c ? `<div class="sa-sub">${esc(sentence(c.group))}</div>` : ""}</div>
      <div class="sa-hr">${researchLine(r, failed)}</div>
      <div class="sa-hr"><div class="cc-label">POSITION</div>${pos}</div>
      <div class="sa-actions">
        ${needAnalyze ? `<button class="cc-btn cc-mini cc-primary cc-analyze-needed" data-rerun>Analyze</button>` : ""}
        <button class="cc-btn cc-mini${needAnalyze ? "" : " cc-primary"}" data-quick-r="${esc(sym)}">Quick check</button>
        ${!needAnalyze && r.freshness === "AGING" ? `<button class="cc-btn cc-mini" data-rerun>Analyze</button>` : ""}
        <button class="cc-btn cc-mini" data-full="${esc(sym)}" aria-expanded="false">Full analysis</button>
        ${owned === true ? `<button class="cc-btn cc-mini cc-quiet" data-review-r="${esc(sym)}">Review</button>` : ""}
        ${window.StrategyFit ? `<button class="cc-btn cc-mini cc-quiet" data-fit-r="${esc(sym)}" title="Compare ${esc(sym)} with your saved strategy rules (Strategy Lab)">Strategy Fit</button>` : ""}
        <button class="cc-btn cc-mini cc-link" data-collapse="${esc(sym)}">Collapse</button></div></div>`;
  }

  // ---- LEFT: layers · why · next -------------------------------------------------------------------------------
  // Concentration numbers are shown once, in the portfolio panel; the Why list keeps the reason without repeating them.
  const trimmed = (t, owned) => (owned && /^(The position is already large|.+ exposure is already high) \(/.test(t) ? t.replace(/ \([^)]*\)\.$/, ".") : t);

  function leftCol(c, x, owned) {
    const lay = x.layers;
    const layer = (name, v) => `<div class="sa-layer"><div class="cc-label">${name}</div><div class="sa-lv"><span class="cc-dot cc-${LAYER[v] || "dim"}"></span>${esc(v)}</div></div>`;
    const cautions = x.all_cautions.map((t) => trimmed(t, owned === true));
    const pat = x.pattern_context;
    return `<div class="sa-col">
      <div class="sa-block"><div class="sa-layers">${layer("MARKET", lay.market)}<span class="sa-arrow">→</span>${layer("STOCK", lay.stock)}<span class="sa-arrow">→</span>${layer("PORTFOLIO", lay.portfolio)}</div>
        <div class="sa-matters"><span class="cc-label">WHY THIS MATTERS</span> ${esc(sentence(lay.summary))}.</div></div>
      <div class="sa-block"><div class="cc-label">WHAT SUPPORTS THIS</div>${list(x.all_supports.slice(0, 3), "cc-li-ok", "Nothing supportive stands out right now.")}
        <div class="cc-label">WHAT MAKES ME CAUTIOUS</div>${list(cautions.slice(0, 3), "cc-li-warn", "No specific cautions right now.")}
        ${pat ? `<div class="sa-pattern" title="${esc(pat.note)}"><b>${esc(pat.title)}</b> ${esc(tidy(pat.text))}</div>` : ""}</div>
      <div class="sa-next"><div class="cc-label">NEXT THING TO CONSIDER</div><p>${esc(c.next)}</p></div></div>`;
  }

  // ---- CENTER: price position · trend/momentum/volume/event · research evidence ------------------------------------
  function priceMap(c, x) {
    const p = n(c.price), s = n(c.watch_next.support), r = n(c.watch_next.resistance);
    const word = x.evidence.price_location;                      // existing label ("Near resistance · extended", …)
    const nearS = /^Near support/.test(word), nearR = /^Near resistance/.test(word);
    const valid = p != null && s != null && r != null && s < r && p >= s && p <= r;
    let body;
    if (valid) {
      const f = (p - s) / (r - s), at = f > 0.8 ? "r" : f < 0.2 ? "l" : "c", left = `${(f * 100).toFixed(1)}%`;
      body = `<div class="sa-map${nearS ? " sa-near-s" : ""}${nearR ? " sa-near-r" : ""}">
        <div class="sa-now-row"><span class="sa-now sa-at-${at}" style="left:${left}">NOW ${money(p)}</span></div>
        <div class="sa-track"><span class="sa-dot sa-dot-s"></span><span class="sa-marker" style="left:${left}"></span><span class="sa-dot sa-dot-r"></span></div>
        <div class="sa-ends"><span class="sa-end-s">Support <b>${money(s)}</b></span><span class="sa-end-r">Resistance <b>${money(r)}</b></span></div></div>`;
    } else {
      // A missing or inconsistent level would make a scale misleading: show the values as labels instead.
      const why = p == null ? "The current price is unavailable."
        : s == null && r == null ? "No recent support or resistance level was found."
          : r == null ? "No recent resistance level above the price was found, so no scale is drawn."
            : s == null ? "No recent support level below the price was found, so no scale is drawn."
              : "The price is outside the recent support–resistance range, so no scale is drawn.";
      body = `<div class="sa-cells3"><div><div class="cc-label">SUPPORT</div><b>${money(s)}</b></div><div><div class="cc-label">CURRENT</div><b>${money(p)}</b></div>
        <div><div class="cc-label">RESISTANCE</div><b>${money(r)}</b></div></div><p class="cc-small cc-dimtext sa-nomap">${esc(why)}</p>`;
    }
    return `<div class="sa-block"><div class="sa-bhead"><span class="cc-label">PRICE POSITION</span>${tag(word, locKind(word))}</div>${body}</div>`;
  }

  function nextEvent(dec, x) {
    const e = x.events.next_event, m = dec.market && dec.market.next_event;
    if (dec.events_note) return `<b>Events loading</b>`;
    if (!x.events.available) return `<b>Event data unavailable</b>`;
    if (e) return `<b>${esc(eventName(e.title))}</b> · ${esc(shortDate(e.date))} · ${esc(days(e.days_until))}`;
    if (m) return `<b>${esc(eventName(m.title))}</b> · ${esc(shortDate(m.date))} · ${esc(days(m.days_until))} <span class="cc-dimtext">(market-wide)</span>`;
    return `No major event is close.`;
  }

  function evidenceBlock(dec) {
    const r = dec.research, f = dec.full_analysis;
    if (!r || !r.available) return `<div class="sa-block"><div class="cc-label">RESEARCH EVIDENCE</div><p class="cc-small cc-dimtext">No research for this stock yet — Analyze creates it.</p></div>`;
    const seg = (v, cls) => (n(v) > 0 ? `<div class="seg ${cls}" style="width:${n(v)}%"></div>` : "");
    const items = f ? f.catalysts.items : [];
    const by = {}; items.forEach((k) => { const s = cap(k.sentiment || "unclassified"); by[s] = (by[s] || 0) + 1; });
    const mix = Object.entries(by).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(" · ");
    return `<div class="sa-block"><div class="sa-bhead"><span class="cc-label">RESEARCH EVIDENCE MIX</span></div>
      <div class="evidence-bar sa-ebar">${seg(r.bullish_pct, "bullish")}${seg(r.neutral_pct, "neutral")}${seg(r.bearish_pct, "bearish")}</div>
      <div class="cc-small">Bullish ${val(r.bullish_pct)}% · Neutral ${val(r.neutral_pct)}% · Bearish ${val(r.bearish_pct)}%
        <span class="cc-dimtext">— a summary of current evidence, not a probability</span></div>
      <div class="sa-cat"><span class="cc-label">CATALYSTS</span> ${f ? `${items.length} relevant item${items.length === 1 ? "" : "s"}${mix ? ` · ${esc(mix)}` : ""}` : "Unavailable"}
        ${items.length ? `<button class="cc-btn cc-mini cc-link" data-open-tab="catalysts">Show catalysts</button>` : ""}</div></div>`;
  }

  const evidenceRight = (dec, x) => dec.owned !== true && !x.conflicts.length;

  function centerCol(dec, c, x) {
    const ev = dec.events_note ? "Loading" : x.evidence.event_risk;
    const cell = (label, v) => `<div class="sa-cell"><div class="cc-label">${label}</div><div class="sa-cv"><span class="cc-dot cc-${WORD[v] || "dim"}"></span>${esc(v)}</div></div>`;
    return `<div class="sa-col">${priceMap(c, x)}
      <div class="sa-strip">${cell("TREND", x.evidence.stock_trend)}${cell("MOMENTUM", x.evidence.momentum)}${cell("VOLUME", c.watch_next.volume)}${cell("EVENT RISK", ev)}</div>
      <div class="sa-evline"><span class="cc-label">NEXT EVENT</span> ${nextEvent(dec, x)}</div>
      ${evidenceRight(dec, x) ? "" : evidenceBlock(dec)}</div>`;
  }

  // ---- RIGHT: portfolio impact · improves / weakens · conflicts ----------------------------------------------------
  function portfolioBlock(dec, c, x) {
    const pol = x.policy, owned = dec.owned;
    const sectorName = x.sector === "UNCLASSIFIED" ? "Unclassified" : x.sector;
    const flag = (sev) => (FIT[sev] ? tag(FIT[sev][0], FIT[sev][1]) : "");
    if (owned === true && c.position) {
      const p = c.position;
      return `<div class="sa-block"><div class="sa-bhead"><span class="cc-label">YOUR POSITION</span><span class="sa-pstate"><span class="cc-dot cc-${LAYER[x.layers.portfolio] || "dim"}"></span>Portfolio ${esc(x.layers.portfolio)}</span></div>
        <div class="sa-kv"><div><span>Portfolio weight</span><b>${pct(p.weight)}</b> ${flag(pol.position_severity)}</div>
          <div><span>Sector exposure</span><b>${esc(sectorName)}${pol.sector_weight != null ? ` · ${pct(pol.sector_weight)}` : ""}</b> ${pol.sector_severity ? flag(pol.sector_severity) : `<span class="cc-small cc-dimtext">no concentration flag</span>`}</div></div>
        <button class="cc-btn cc-mini cc-link" data-sa-toggle="pf">Show portfolio details</button>
        <div class="sa-more" data-sa-panel="pf" hidden><div class="sa-kv">
          <div><span>Market value</span><b>${money(p.value)}</b></div><div><span>Average cost</span><b>${money(p.avg_cost)}</b></div>
          <div><span>Cost basis</span><b>${money(p.cost_basis)}</b></div><div><span>Quote quality</span><b>${val(p.quote_quality)}</b></div>
          <div><span>Portfolio fit</span><b>${val(x.evidence.portfolio_fit)}</b></div><div><span>Sector (${esc(sessionWord())})</span><b>${x.sector_pct_today == null ? "Unavailable" : spct(x.sector_pct_today)}</b></div></div></div></div>`;
    }
    const secNote = pol.sector_severity ? `<div class="sa-kv"><div><span>Sector</span><b>${esc(sectorName)}</b> ${flag(pol.sector_severity)}</div></div>
      <p class="cc-small cc-dimtext">Your ${esc(sectorName)} exposure already carries a portfolio flag.</p>` : `<div class="sa-kv"><div><span>Sector</span><b>${esc(sectorName)}</b></div></div>`;
    return `<div class="sa-block"><div class="sa-bhead"><span class="cc-label">PORTFOLIO FIT</span><span class="sa-pstate"><span class="cc-dot cc-${LAYER[x.layers.portfolio] || "dim"}"></span>Portfolio ${esc(x.layers.portfolio)}</span></div>
      <div class="sa-hv">${owned === false ? "Not currently owned" : "Ownership unknown"}</div>
      ${owned === false ? secNote : `<p class="cc-small cc-dimtext">Robinhood is not connected, so the portfolio layer is unavailable (not assumed safe).</p>`}</div>`;
  }

  const waiting = (dec, c) => dec.owned !== true && c.waiting_for.length > 0;

  function rightCol(dec, c, x) {
    const first = waiting(dec, c) ? `<div class="cc-label">WHAT I'M WAITING FOR</div>${list(c.waiting_for, "sa-li-wait")}`
      : `<div class="cc-label">SETUP IMPROVES IF</div>${list(x.improves_if.slice(0, 3), "cc-li-ok")}`;
    return `<div class="sa-col">${portfolioBlock(dec, c, x)}
      <div class="sa-iw"><div>${first}</div>
        <div><div class="cc-label">SETUP WEAKENS IF</div>${list(x.weakens_if.slice(0, 3), "dr-li-x")}</div></div>
      ${evidenceRight(dec, x) ? evidenceBlock(dec) : ""}
      ${x.conflicts.length ? `<div class="sa-block sa-conflicts"><div class="cc-label">CONFLICTING EVIDENCE</div>${x.conflicts.map((k) => `<div class="sa-conflict">
        <div class="sa-ab">${esc(k.a)} <span>↔</span> ${esc(k.b)}</div><div>${esc(tidy(k.text))}</div></div>`).join("")}</div>` : ""}</div>`;
  }

  // Everything beyond the capped lists, plus the rule that set the view — shown only on request (no request made).
  function moreHtml(dec, c, x) {
    const extra = (a, k) => a.slice(k), w = waiting(dec, c);
    const cautions = x.all_cautions.map((t) => trimmed(t, dec.owned === true));
    const rows = [["More supporting evidence", extra(x.all_supports, 3), "cc-li-ok"], ["More cautions", extra(cautions, 3), "cc-li-warn"],
      [w ? "Setup improves if" : "Also improves if", extra(x.improves_if, w ? 0 : 3), "cc-li-ok"], ["Also weakens if", extra(x.weakens_if, 3), "dr-li-x"]].filter((r) => r[1].length);
    return `${rows.map(([t, a, cls]) => `<div><div class="cc-label">${esc(t)}</div>${list(a, cls)}</div>`).join("")}
      <div><div class="cc-label">RULE THAT SET THIS VIEW</div><p class="cc-small">${esc(x.rule)}</p></div>`;
  }
  const moreCount = (dec, c, x) => Math.max(0, x.all_supports.length - 3) + Math.max(0, x.all_cautions.length - 3) +
    Math.max(0, x.improves_if.length - (waiting(dec, c) ? 0 : 3)) + Math.max(0, x.weakens_if.length - 3);

  function decisionHtml(dec) {
    const c = dec && dec.decision;
    if (!c) return `<p class="cc-small">The current view is unavailable right now.</p>`;
    const x = c.details;
    return `<div class="cc-result-grid sa-grid">${leftCol(c, x, dec.owned)}${centerCol(dec, c, x)}${rightCol(dec, c, x)}</div>
      <div class="sa-foot"><button class="cc-btn cc-mini cc-link" data-sa-toggle="more">Show details${moreCount(dec, c, x) ? ` (+${moreCount(dec, c, x)})` : ""}</button>
        <span class="cc-note">${esc(dec.note || "")}</span></div>
      <div class="sa-more sa-more-grid" data-sa-panel="more" hidden>${moreHtml(dec, c, x)}</div>`;
  }

  // ---- FULL ANALYSIS: secondary, one tab at a time, from data already in memory -------------------------------------
  function tabBody(dec, k) {
    const f = dec.full_analysis, c = dec.decision, x = c && c.details;
    const kv = (rows) => `<div class="sa-kv sa-kv4">${rows.map(([a, b]) => `<div><span>${esc(a)}</span><b>${b}</b></div>`).join("")}</div>`;
    const none = (t) => `<p class="cc-small cc-dimtext">${esc(t)}</p>`;
    if (k === "technical") {
      const t = (x && x.technicals) || {};
      if (!x || !x.technicals) return none("Technical data is unavailable for this stock right now.");
      return `${kv([["Trend", val(x.evidence.stock_trend)], ["Momentum", `${val(x.evidence.momentum)} · score ${val(t.momentum_score)}`],
        ["5-day change", spct(t.momentum_5d_pct)], ["RSI (14)", val(t.rsi)], ["EMA 9 / 20 / 50", `${val(t.ema_9)} / ${val(t.ema_20)} / ${val(t.ema_50)}`],
        ["Relative volume", t.relative_volume == null ? "Unavailable" : `${esc(t.relative_volume)}x`], ["ATR", val(t.atr)],
        ["Volatility", f && f.volatility.volatility_pct != null ? `${esc(f.volatility.volatility_pct)}%` : "Unavailable"],
        ["From support", spct(t.dist_from_support_pct)], ["From resistance", spct(t.dist_from_resistance_pct)],
        ["20-day range", f && f.levels.high_20d != null ? `${money(f.levels.low_20d)} – ${money(f.levels.high_20d)}` : "Unavailable"],
        ["Data as of", esc(when(t.as_of))]])}${none("Raw indicator values behind the plain labels above. The same data the current view uses.")}`;
    }
    if (k === "catalysts") {
      if (!f) return none("No research is available, so there are no catalysts to show.");
      const items = f.catalysts.items;
      return `${items.length ? `<ul class="sa-cats">${items.map((i) => `<li><div><b>${esc(i.title)}</b> ${tag(cap(i.sentiment || "Unclassified"), i.sentiment === "POSITIVE" ? "ok" : i.sentiment === "NEGATIVE" ? "alert" : "dim")}</div>
        <div class="cc-small cc-dimtext">${esc(i.source)} · ${esc(when(i.published_at))}</div>${i.reason ? `<div class="cc-small">${esc(i.reason)}</div>` : ""}</li>`).join("")}</ul>`
        : none(f.catalysts.note || "No verified catalysts in this research.")}${none("Headlines are context from the research; this app does not claim they caused a price move.")}`;
    }
    if (k === "risks") {
      if (!f) return none("No research is available, so there are no risk flags to show.");
      return `${f.risk.flags.length ? `<ul class="cc-list">${f.risk.flags.map((i) => `<li class="cc-li-warn">${esc(i.description)} <span class="cc-small cc-dimtext">(${esc(cap(i.severity))})</span></li>`).join("")}</ul>` : none("No risk flags in this research.")}
        ${f.risk.explanation_ai ? `<p class="cc-small"><b>AI risk explanation:</b> ${esc(f.risk.explanation_ai)}</p>` : ""}`;
    }
    if (k === "market") {
      const m = dec.market || {};
      return kv([["Market conditions", m.available ? esc(cap(m.conditions)) : "Unavailable"], ["Market trend", m.available ? val(m.verdicts.market_trend) : "Unavailable"],
        ["Market risk", m.available ? val(m.verdicts.risk_level) : "Unavailable"], ["Sector", x ? val(x.evidence.sector) : "Unavailable"],
        [`Sector (${sessionWord()})`, x && x.sector_pct_today != null ? spct(x.sector_pct_today) : "Unavailable"],
        ["Sector ETF", f && f.sector ? `${val(f.sector.etf)} ${f.sector.pct_change != null ? spct(f.sector.pct_change) : ""}` : "Unavailable"]]);
    }
    if (k === "portfolio") {
      if (!x) return none("Unavailable.");
      const p = c.position || {};
      return kv([["Ownership", dec.owned === true ? "You own this" : dec.owned === false ? "Not owned" : "Unknown (Robinhood not connected)"],
        ["Portfolio layer", val(x.layers.portfolio)], ["Portfolio fit", val(x.evidence.portfolio_fit)],
        ["Position flag", val(x.policy.position_severity || "No flag")], ["Sector flag", val(x.policy.sector_severity || "No flag")],
        ["Sector weight", x.policy.sector_weight == null ? "Unavailable" : pct(x.policy.sector_weight)],
        ...(dec.owned === true ? [["Average cost", money(p.avg_cost)], ["Cost basis", money(p.cost_basis)]] : [])]);
    }
    if (k === "evidence") {
      const e = f && f.evidence, h = x && x.historical_outcomes;
      return `${e ? kv([["Research view", val(cap(e.research_view))], ["Evidence mix", `Bullish ${val(e.bullish_pct)}% · Neutral ${val(e.neutral_pct)}% · Bearish ${val(e.bearish_pct)}%`],
        ...e.categories.map((i) => [`${cap(i.category)} evidence`, i.score == null ? "n/a" : esc(i.score)]),
        ["Data quality", `${val(cap(f.data_quality.level))}`]]) : none("No research evidence is available.")}
        ${f && f.data_quality.explanation ? none(f.data_quality.explanation) : ""}
        ${none("Evidence percentages summarize the research inputs; they are not probabilities of future returns.")}
        ${h ? `<p class="cc-small"><b>Past saved research (${esc(h.horizon_trading_days)} trading days, ${esc(h.n)} outcomes):</b> ${h.enough_sample
          ? `positive ${esc(h.positive_return_frequency_pct)}% of the time, median ${spct(h.median_return_pct)}` : "sample too small to summarize"}. ${esc(h.note)}</p>` : ""}`;
    }
    const a = f && f.narrative_ai;
    return a ? `<p class="cc-small"><b>AI research summary</b> — separate from the current view above; it cannot change it.</p>
      <p>${esc(a.whats_happening)} ${esc(a.why)}</p>
      <div class="sa-iw"><div><div class="cc-label">GOOD</div>${list(a.whats_good || [], "cc-li-ok")}</div><div><div class="cc-label">BE CAREFUL</div>${list(a.be_careful || [], "cc-li-warn")}</div></div>`
      : none("No AI summary is available for this research (saved snapshot, or the AI part was unavailable).");
  }

  function fullHtml(dec, tab) {
    const f = dec && dec.full_analysis;
    const count = (k) => (!f ? "" : k === "catalysts" ? ` (${f.catalysts.items.length})` : k === "risks" ? ` (${f.risk.flags.length})` : "");
    return `<div class="cc-full sa-full"><div class="sa-tabs" role="tablist">${TABS.map(([k, t]) => `<button class="sa-tab${k === tab ? " sa-on" : ""}" data-tab="${k}" role="tab" aria-selected="${k === tab}">${esc(t)}${count(k)}</button>`).join("")}</div>
      <div class="sa-tabbody">${tabBody(dec || {}, tab)}</div>
      <div class="cc-small cc-dimtext">Source: ${!f ? "no saved research" : f.source === "FRESH_RESEARCH" ? "research from this session" : "latest saved research snapshot"} (no new AI call).</div></div>`;
  }

  function panelHtml(sym) {
    const { dec, status } = results.get(sym);
    const failed = status.kind === "failed";
    const calls = status.calls == null ? "" : `${status.calls} new AI call${status.calls === 1 ? "" : "s"}`;
    return `<div class="cc-result sa" data-sa="${esc(sym)}">
      <div class="cc-result-status sa-status cc-${failed ? "warn" : "ok"}"><b>${esc(status.title)}</b>${calls ? ` · ${esc(calls)}` : ""}${failed ? ` · <span>Reason: ${esc(status.reason)}</span>` : ""}</div>
      ${headHtml(sym, dec, failed)}
      ${dec && dec.events_note ? `<div class="cc-banner cc-warn">${esc(dec.events_note)}</div>` : ""}
      ${decisionHtml(dec)}
      <div class="cc-full-host" hidden></div></div>`;
  }

  function show(host, sym, reveal) {
    host.innerHTML = panelHtml(sym);
    const w = wrapperOf(host);
    if (w) { w.classList.add("cc-expanded"); if (w.title) { w.dataset.title = w.title; w.removeAttribute("title"); } }   // no card tooltip over the panel
    const dec = results.get(sym).dec, box = host.querySelector(".cc-full-host"), fullBtn = host.querySelector("[data-full]");
    const openTab = (tab) => { box.innerHTML = fullHtml(dec, tab); box.hidden = false; fullBtn.setAttribute("aria-expanded", "true"); fullBtn.textContent = "Hide full analysis"; };
    host.querySelector("[data-collapse]").addEventListener("click", () => collapse(sym));          // no request
    fullBtn.addEventListener("click", () => {                                                        // no request
      if (box.hidden) openTab("technical");
      else { box.hidden = true; fullBtn.setAttribute("aria-expanded", "false"); fullBtn.textContent = "Full analysis"; }
    });
    host.firstElementChild.addEventListener("click", (ev) => {                                     // tabs, toggles: no request
      const t = ev.target.closest("[data-tab], [data-open-tab], [data-sa-toggle]");
      if (!t) return;
      if (t.dataset.tab || t.dataset.openTab) { openTab(t.dataset.tab || t.dataset.openTab); if (t.dataset.openTab) box.scrollIntoView({ block: "nearest" }); return; }
      const panel = host.querySelector(`[data-sa-panel="${t.dataset.saToggle}"]`);
      panel.hidden = !panel.hidden;
      t.textContent = t.textContent.replace(/^(Show|Hide)/, panel.hidden ? "Show" : "Hide");
    });
    host.querySelector("[data-quick-r]").addEventListener("click", () => window.TraderReview && window.TraderReview.quick(sym, 500));
    const rv = host.querySelector("[data-review-r]");
    if (rv) rv.addEventListener("click", () => window.TraderReview && window.TraderReview.openPosition(sym));
    const fit = host.querySelector("[data-fit-r]");                                           // Stage 3.4: one shared view
    if (fit) fit.addEventListener("click", () => window.StrategyFit && window.StrategyFit.openFor(sym));
    host.querySelectorAll("[data-rerun]").forEach((b) => b.addEventListener("click", () => rerun(host)));
    openSym = sym;
    if (reveal && w) {                                   // bring the header into view when the panel first opens
      const top = w.getBoundingClientRect().top;
      if (top < 0 || top > window.innerHeight * 0.45) window.scrollTo({ top: window.scrollY + top - 8 });
    }
  }

  async function failure(host, sym, reason, calls) {
    // the view from the EXISTING research (previous analysis id, or the saved snapshot) — 0 AI calls
    const dec = await post("/api/insights/stock-decision", { symbol: sym, analysis_id: window.ResearchIds.get(sym) }).catch(() => null);
    results.set(sym, { dec, status: { kind: "failed", title: "ANALYSIS COULD NOT BE COMPLETED", reason, calls } });
    show(host, sym, true);
  }

  async function analyze(sym, host, opts) {
    opts = opts || {};
    if (openSym && openSym !== sym) collapse(openSym);                  // one expanded result at a time
    const reopenOnCancel = openSym === sym && results.has(sym);         // re-run from an open panel
    host.innerHTML = `<p class="hint">Checking AI capacity for ${esc(sym)} (no AI calls)…</p>`;
    const plan = await post("/api/insights/daily-review/research-plan", { symbols: [sym] }).catch(() => null);
    if (!plan) return failure(host, sym, "The AI capacity check is unavailable right now.", 0);
    const capacity = `Hour ${plan.calls_last_hour} / ${plan.hourly_limit} · Today ${plan.calls_today} / ${plan.daily_limit}`;
    if (!plan.analyze_now.length) {
      return failure(host, sym, `${LIMIT[plan.limited_by] || "No AI capacity"} (${capacity}). Nothing was run.`, 0);
    }
    const est = plan.calls_for_now;
    host.innerHTML = `<div class="cc-confirm dr-confirm"><div class="cc-label">ANALYZE ${esc(sym)}</div>
      <p>This may use up to ${esc(est)} new AI analysis call${est === 1 ? "" : "s"} (analyses still in the AI cache count as zero).</p>
      <p class="cc-small">AI capacity: ${esc(capacity)}</p>
      <button class="cc-btn cc-primary" data-yes>Analyze</button> <button class="cc-btn" data-no>Cancel</button></div>`;
    host.querySelector("[data-no]").addEventListener("click", () => { if (reopenOnCancel) show(host, sym); else collapse(sym); });
    host.querySelector("[data-yes]").addEventListener("click", async () => {
      host.innerHTML = `<div class="cc-result-status cc-info"><b>ANALYZING ${esc(sym)}…</b><div class="cc-small">This may use up to ${esc(est)} AI analysis call${est === 1 ? "" : "s"}.</div></div>`;
      const before = await aiCallsToday();
      let res;
      try { res = await window.ResearchIds.analyzeFull(sym); }
      catch (e) { return failure(host, sym, e.message || "Research unavailable", null); }
      const after = await aiCallsToday();
      const calls = before != null && after != null ? Math.max(0, after - before) : null;
      const gated = aiUnavailable(res);
      if (gated) return failure(host, sym, gated, calls);               // not stored: never presented as refreshed
      window.ResearchIds.set(sym, res.analysis_id);
      const dec = await post("/api/insights/stock-decision", { symbol: sym, analysis_id: res.analysis_id }).catch(() => null);
      results.set(sym, { dec, status: calls === 0 ? { kind: "cached", title: "✓ CURRENT ANALYSIS LOADED", calls: 0 }
        : { kind: "updated", title: "✓ ANALYSIS UPDATED", calls } });
      show(host, sym, true);
      if (opts.onResearch && dec && dec.research) opts.onResearch(sym, dec.research, dec.decision);
    });
  }

  // Re-attach the open result after the dashboard re-renders (uses stored data; no request).
  function reopen(root) {
    if (!openSym || !results.has(openSym)) return;
    const host = root.querySelector(`[data-result-for="${openSym}"]`);
    if (host) show(host, openSym);
  }

  window.StockResult = { analyze, collapse, reopen, stateTag, stateKind, age, freshTag, tidy, days, sessionWord };
})();
