// command_center.js — Stage 2.7F BEGINNER COMMAND CENTER (the Beginner Dashboard).
//
// One screen: TODAY'S MARKET · MY ROBINHOOD · MY STOCKS TODAY · OPPORTUNITIES TO RESEARCH · RISKS & EVENTS ·
// WHAT TO WATCH NEXT. Every label and number comes from the backend (deterministic Python); the browser only
// formats. No AI runs on page load — research runs only after an explicit Analyze click and confirmation.
// Colors: green/red only for observed price movement; amber for caution; blue/grey for information.
// Nothing here can place, prepare or cancel an order.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const signed = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${money(Math.abs(n(v)))}`);
  const spct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${Math.abs(n(v)).toFixed(2)}%`);
  const pct = (v) => (n(v) == null ? "N/A" : `${n(v).toFixed(2)}%`);
  const pts = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${Math.abs(n(v)).toFixed(2)} pts`);
  const move = (v) => (n(v) == null ? "" : n(v) > 0 ? "pct-up" : n(v) < 0 ? "pct-down" : "");
  const time = (v) => (v ? new Date(v).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "N/A");
  const when = (v) => (v ? new Date(v).toLocaleString() : "N/A");
  const cap = (s) => (s ? String(s).charAt(0) + String(s).slice(1).toLowerCase() : "");
  const tag = (text, kind) => `<span class="cc-tag cc-${esc(kind)}">${esc(text)}</span>`;
  const FRESH = { FRESH: "ok", AGING: "warn", STALE: "dim", UNKNOWN: "dim", MISSING: "dim" };   // 2.8C: grey = stale/unknown
  const fresh = (f, extra) => (f ? `<span class="cc-fresh cc-${FRESH[f.label || f] || "dim"}" title="${esc(f.updated ? `Updated ${when(f.updated)}` : "")}">${esc(f.label || f)}${extra ? ` · ${esc(extra)}` : ""}</span>` : "");
  const LEVEL = { good: "ok", mid: "warn", bad: "alert", unknown: "dim", neutral: "dim" };
  const VERDICT = { Improving: "ok", Mixed: "warn", Weakening: "bad", "More Stable": "ok", Selective: "warn", "More Challenging": "alert", Lower: "ok", Normal: "info", Elevated: "alert" };
  const COND = { SUPPORTIVE: "ok", MIXED: "warn", CAUTIOUS: "alert" };
  const ATT = { Normal: "info", Watch: "warn", "Higher attention": "alert", NORMAL: "info", MEDIUM: "warn", HIGH: "alert" };
  const ATT_TEXT = { NORMAL: "Normal", MEDIUM: "Watch", HIGH: "Higher attention" };
  const LOC = { NEAR_SUPPORT: "Near support", MIDDLE_OF_RANGE: "Mid-range", NEAR_RESISTANCE: "Near resistance", NO_RESISTANCE_ABOVE: "Above recent resistance", NO_SUPPORT_BELOW: "Below recent support", UNAVAILABLE: "Location unavailable", EXTENDED: "Extended (after a fast rise)" };
  const TREND = { UPTREND: "Up", DOWNTREND: "Down", MIXED: "Mixed" };
  const arrow = (t) => ({ UPTREND: "↗", Up: "↗", DOWNTREND: "↘", Down: "↘" }[t] || "→");
  let last = null, busy = false;
  let batchResult = null;   // 2.7G.2: last "Analyze my holdings" result, kept in memory only

  async function getJSON(url, options) { const r = await fetch(url, options); return r.json(); }
  const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  // ---- research ids (this browser tab only) --------------------------------------------------------------------
  const ResearchIds = {
    all() { try { return JSON.parse(sessionStorage.getItem("analysisIds") || "{}"); } catch (e) { return {}; } },
    get(sym) { return this.all()[sym] || null; },
    set(sym, id) { try { const a = this.all(); a[sym] = id; sessionStorage.setItem("analysisIds", JSON.stringify(a)); } catch (e) { /* ignore */ } },
    // Runs the existing Stage 2 research (the only AI-spending action here). Always behind an explicit confirmation.
    async analyze(sym) {
      const r = await fetch(`/api/stocks/${encodeURIComponent(sym)}/research`);
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.analysis_id) throw new Error(d.detail || `HTTP ${r.status}`);
      this.set(sym, d.analysis_id);
      return d.analysis_id;
    },
    // 2.7G.3: same endpoint, but returns the whole response and does NOT store the id (see stock_result.js).
    async analyzeFull(sym) {
      const r = await fetch(`/api/stocks/${encodeURIComponent(sym)}/research`);
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.analysis_id) throw new Error(d.detail || `HTTP ${r.status}`);
      return d;
    },
  };
  window.ResearchIds = ResearchIds;

  // Inline confirmation (no browser dialogs): resolves true on Continue.
  function confirmBox(host, text) {
    return new Promise((resolve) => {
      host.innerHTML = `<div class="cc-confirm" role="alertdialog"><p>${esc(text)}</p>
        <button class="cc-btn cc-primary" data-yes>Continue</button> <button class="cc-btn" data-no>Cancel</button></div>`;
      host.querySelector("[data-yes]").addEventListener("click", () => { host.innerHTML = ""; resolve(true); });
      host.querySelector("[data-no]").addEventListener("click", () => { host.innerHTML = ""; resolve(false); });
    });
  }

  // ---- TODAY'S MARKET — Stage 2.8A/B compact strip (same data; details, news and prose behind toggles) -------------
  const label = (s) => String(s || "").split(":")[0].replace(/\.$/, "");        // "Weakening momentum: …" -> "Weakening momentum"
  const firstSentence = (s) => { const t = String(s || ""); const i = t.indexOf(". "); return i > 0 ? t.slice(0, i + 1) : t; };
  const eventName = (t) => { const m = /\(([^)]+)\)/.exec(t || ""); return m ? m[1] : (t || ""); };
  const shortDate = (iso) => { const d = iso ? new Date(`${iso}T12:00:00`) : null; return d && !isNaN(d) ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : (iso || ""); };
  const cell = (lab, value, sub, cls) => `<div class="cc-m"><div class="cc-label">${esc(lab)}</div><div class="cc-mv ${cls || ""}">${value}</div>${sub ? `<div class="cc-small cc-dimtext">${sub}</div>` : ""}</div>`;
  const sentenceCase = (t) => (t ? String(t).charAt(0).toUpperCase() + String(t).slice(1) : "");
  // 2.8E buttons: primary = cc-primary · secondary = plain (Details, Review, Refresh) · tertiary = cc-link (Why?, More)
  const toggle = (id, text, tertiary) => `<button class="cc-btn cc-mini${tertiary ? " cc-link" : ""}" data-toggle="${id}" aria-expanded="false">${text}</button>`;
  const T = (t) => (window.StockResult ? window.StockResult.tidy(t) : String(t == null ? "" : t));        // wording tidy only
  const dayz = (v) => (window.StockResult ? window.StockResult.days(v) : `${v} days`);
  const sess = () => (window.StockResult ? window.StockResult.sessionWord() : "today");

  function hero(m) {
    if (!m.available) {
      return `<section class="cc-card cc-hero"><div class="cc-head"><h2>TODAY'S MARKET</h2><button class="cc-btn cc-mini" data-cc-refresh>Refresh</button></div>
        <div class="cc-banner cc-warn">${esc(m.message)}</div><p class="hint">Nothing is guessed while market data is unavailable.</p></section>`;
    }
    const v = m.verdicts, b = m.breadth, nxt = m.next_event, adv = n(b.advancing_pct);
    const verdict = (lab, value) => cell(lab, `<span class="cc-dot cc-${VERDICT[value] || "info"}"></span>${esc(value)}`);
    const tile = (t) => cell(t.symbol, t.available ? `<span class="${move(t.pct)}">${spct(t.pct)}</span>` : "N/A",
      t.available ? `5D <span class="${move(t.pct_5d)}">${spct(t.pct_5d)}</span> · ${esc(arrow(t.trend))} ${esc(TREND[t.trend] || "N/A")}` : "unavailable");
    const cautions = m.data_says.caution.map(label), positives = m.data_says.positive.length;
    return `<section class="cc-card cc-hero cc-compact">
      <div class="cc-head"><h2>TODAY'S MARKET ${tag(m.conditions, COND[m.conditions] || "info")}</h2>
        <div class="cc-head-tools">${fresh(m.freshness, time(m.freshness.updated))}
          ${toggle("cc-mkt-details", "Market details")}${toggle("cc-news-panel", `News (${m.news.length})`)}
          <button class="cc-btn cc-mini" id="cc-explain-btn">Explain (AI)</button>
          <button class="cc-btn cc-mini" data-cc-refresh>Refresh</button></div></div>
      ${m.stale_message && m.freshness.label === "STALE" ? `<div class="cc-banner cc-warn">${esc(m.stale_message)}</div>` : ""}
      <div class="cc-mstrip">
        ${verdict("Market trend", v.market_trend)}${verdict("Environment", v.trading_environment)}${verdict("Risk", v.risk_level)}
        ${m.tiles.map(tile).join("")}
        ${cell("Breadth", adv == null ? "N/A" : `${esc(b.advancing_pct)}% rising`, adv == null ? esc(b.label) : `${esc(cap(b.label))} <span class="cc-bar cc-bar-sm" title="${esc(b.advancing_pct)}% of ${esc(b.scope || "the tracked list")} rising"><span style="width:${Math.max(0, Math.min(100, adv))}%"></span></span>`)}
        ${cell("Volatility", esc({ LOW: "Low", NORMAL: "Normal", ELEVATED: "Elevated" }[m.volatility] || m.volatility))}
        ${cell("Next event", nxt ? esc(eventName(nxt.title)) : "None soon", nxt ? `${esc(shortDate(nxt.date))} · ${esc(nxt.days_until)} days` : "")}
      </div>
      <div class="cc-oneline"><span>${esc(firstSentence(m.big_picture))}</span>
        ${cautions.map((c) => `<span class="cc-chipline cc-warn">⚠ ${esc(c)}</span>`).join("")}
        ${positives ? `<span class="cc-chipline cc-ok">✓ ${positives} supportive signal${positives === 1 ? "" : "s"}</span>` : ""}</div>
      <div id="mk-explain"></div>
      <div class="cc-panel" id="cc-mkt-details" hidden>
        <p class="cc-small">${esc(m.big_picture)}</p>
        <ul class="cc-list">${m.data_says.positive.map((x) => `<li class="cc-li-ok">${esc(x)}</li>`).join("")}${m.data_says.caution.map((x) => `<li class="cc-li-warn">${esc(x)}</li>`).join("")}</ul>
        <p class="cc-small cc-dimtext">Breadth: ${esc(b.advancing_pct)}% rising · ${esc(b.above_ema20_pct)}% above their 20-day average — ${esc(b.scope || "")}</p>
        <div class="cc-more-body" data-lazy="market">Loading…</div></div>
      <div class="cc-panel" id="cc-news-panel" hidden>
        ${m.news.length ? m.news.map((x) => `<div class="cc-news"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>
          <div class="cc-small">${esc(x.source)} · ${esc(when(x.time))} · Affects: ${esc(x.affected)}</div><div class="cc-small cc-dimtext">${esc(x.why_it_may_matter)}</div></div>`).join("")
          : `<p class="cc-small">No sourced headlines were returned.</p>`}
        <p class="cc-note">${esc(m.causation_note)}</p></div>
    </section>`;
  }

  // ---- MY ROBINHOOD — one metric strip ----------------------------------------------------------------------------
  // 2.8D: one compact line by default; the reconnect steps and technical details stay behind "How to reconnect"
  function notConnected(r) {
    return `<section class="cc-card cc-rh cc-compact"><div class="cc-head"><h2>MY ROBINHOOD ${tag("NOT CONNECTED", "warn")}</h2>
        <div class="cc-head-tools"><span class="cc-small cc-dimtext">Market data keeps working · ownership and portfolio fit are unknown</span>
          ${toggle("cc-rh-fix", "How to reconnect")}<button class="cc-btn cc-mini" data-cc-refresh>Refresh</button></div></div>
      <div class="cc-panel cc-down cc-down-compact" id="cc-rh-fix" hidden><div><h3>${esc(r.title || "ROBINHOOD NOT CONNECTED")}</h3><p class="cc-small">${esc(r.message)} ${esc(r.note || "")}</p></div>
        <div><div class="cc-label">HOW TO FIX</div><ol class="cc-steps">${(r.how_to_fix || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        <details class="cc-more"><summary>Technical details</summary><div class="cc-small">Reason: ${esc(r.technical && r.technical.reason)}<br>${esc(r.technical && r.technical.detail)}</div></details></div></div></section>`;
  }

  function robinhood(d) {
    const r = d.robinhood;
    if (!r.connected) return notConnected(r);
    const s = r.summary, sess = d.session.label;
    const sec = s.largest_sector;
    return `<section class="cc-card cc-rh cc-compact"><div class="cc-head"><h2>MY ROBINHOOD <span class="cc-small cc-dimtext">${esc(s.account.alias)} ${esc(s.account.masked_id)}</span></h2>
        <div class="cc-head-tools">${fresh(s.freshness, time(s.freshness.updated))} <button class="cc-btn cc-mini" id="cc-view-portfolio">View portfolio</button></div></div>
      <div class="cc-mstrip cc-mstrip-7">
        ${cell("Account value", money(s.account_value), "Reported by Robinhood", "cc-mv-lg")}
        ${cell(sess, `${signed(s.session_change)} <span class="cc-small">(${spct(s.session_pct)})</span>`, `Calculated <button class="cc-i" type="button" aria-label="How is this calculated?">ⓘ</button><span class="cc-tip">${esc(s.session_basis)}</span>`, `cc-mv-lg ${move(s.session_change)}`)}
        ${cell("Open P&L", `${signed(s.open_pnl)} <span class="cc-small">(${spct(s.open_pnl_pct)})</span>`, "On what holdings cost", `cc-mv-lg ${move(s.open_pnl)}`)}
        ${cell("Cash", money(s.cash), "")}
        ${cell("Largest", s.largest_position ? `${esc(s.largest_position.symbol)} · ${pct(s.largest_position.weight)}` : "N/A", "position")}
        ${cell("Sector", sec ? `${esc(sec.sector)} · ${pct(sec.weight)}` : "N/A", "largest verified")}
        ${cell("Attention", tag(ATT_TEXT[s.attention] || s.attention, ATT[s.attention] || "info"), "portfolio rules")}
      </div></section>`;
  }

  // ---- WHAT NEEDS ATTENTION — the existing deterministic observations, one short chip each ------------------------
  // Each chip is the same observation as a d.feedback sentence (same order); the full sentences stay behind "Why?".
  function chipFor(text, d) {
    let x;
    if ((x = /^(.+?) exposure is high \(([\d.]+)%\)/.exec(text))) return ["alert", `${x[1]} ${x[2]}%`, "High concentration"];
    if ((x = /^(\S+) is your largest position \(([\d.]+)%\)/.exec(text))) return ["warn", `${x[1]} ${x[2]}%`, "Largest position — biggest effect on your account"];
    if ((x = /^(.+?) is on (\d{4}-\d\d-\d\d) \(([\d.]+) days away\)/.exec(text))) return ["warn", eventName(x[1]), `${shortDate(x[2])} · ${x[3]} days`];
    if (/quote or research is missing or old/.test(text)) {
      const cards = d.my_stocks || [], cur = cards.filter((c) => c.research.freshness === "FRESH" || c.research.freshness === "AGING").length;
      return ["warn", `Research ${cur}/${cards.length} current`, `${cards.length - cur} missing or old`];
    }
    if ((x = /^(.+?) (?:is|are) near resistance or moved up quickly/.exec(text))) return ["warn", x[1].replace(/, /g, " / "), "Near resistance / moved quickly"];
    if ((x = /^(\S+) was the biggest drag on your account this session \((.+)\)/.exec(text))) return ["info", `${x[1]} ${x[2]}`, "Biggest drag"];
    return ["info", text.length > 42 ? `${text.slice(0, 40)}…` : text, ""];
  }

  function attention(d) {
    if (!d.feedback || !d.feedback.length) return "";
    const chips = d.feedback.slice(0, 5).map((t) => chipFor(t, d));
    return `<section class="cc-card cc-focus cc-compact"><div class="cc-head"><h2>WHAT NEEDS ATTENTION</h2>
        <div class="cc-head-tools">${toggle("cc-attn-why", "Why?", true)}</div></div>
      <div class="cc-chips">${chips.map(([k, t, sub]) => `<div class="cc-chip cc-${k}"><div class="cc-chip-t">${esc(t)}</div>${sub ? `<div class="cc-small cc-dimtext">${esc(sub)}</div>` : ""}</div>`).join("")}</div>
      <div class="cc-panel" id="cc-attn-why" hidden><ol class="cc-focus-list">${d.feedback.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
        <p class="cc-note">Observations from your holdings and today's data — not instructions to buy or sell.</p></div></section>`;
  }

  // ---- MY STOCKS TODAY -----------------------------------------------------------------------------------------
  function vsMarket(d) {
    const v = d.vs_market, c = d.contributors;
    if (!v) return "";
    // 2.8D: the three numbers are in the PORTFOLIO TODAY strip; this panel is the "why" behind them
    const top = v.available ? "" : `<p class="cc-small">Portfolio-vs-market comparison unavailable for this session.</p>`;
    const row = (x) => `<li><strong>${esc(x.symbol)}</strong> <span class="${move(x.change)}">${signed(x.change)}</span> <span class="cc-small cc-dimtext">(${pts(x.contribution_pp)} of your change)</span></li>`;
    const contrib = c ? `<div class="cc-two"><div><h3>HELPED MOST</h3>${c.positive.length ? `<ul class="cc-list">${c.positive.map(row).join("")}</ul>` : "<p class='cc-small'>None this session.</p>"}</div>
      <div><h3>HURT MOST</h3>${c.negative.length ? `<ul class="cc-list">${c.negative.map(row).join("")}</ul>` : "<p class='cc-small'>None this session.</p>"}</div></div>` : "";
    return `<div class="cc-sub cc-vs-why"><h3>WHY YOUR PORTFOLIO MOVED DIFFERENTLY — ${esc(d.session.label)}</h3>${top}
      ${v.why_it_may_differ.length ? `<div class="cc-label">WHY IT MAY DIFFER</div><ul class="cc-list">${v.why_it_may_differ.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <p class="cc-note">${esc(v.note)}${d.session_change && d.session_change.excluded.length ? ` Not included: ${d.session_change.excluded.map((e) => `${esc(e.symbol)} (${esc(e.reason)})`).join(", ")}.` : ""}</p>${contrib}</div>`;
  }

  // 2.8D: PORTFOLIO TODAY — one compact strip (you vs SPY/QQQ). Helped/hurt, exposure and the full comparison stay
  // behind "Why is it different?" (already in this response: opening it makes no request).
  function vsLine(d) {
    const v = d.vs_market;
    if (!v || !v.available) return `<span class="cc-small cc-dimtext">Portfolio-vs-market unavailable this session</span>`;
    return `<span class="cc-pt"><span class="cc-label">${d.session && d.session.is_today === false ? "PORTFOLIO · LAST SESSION" : "PORTFOLIO TODAY"}</span>
      <span>You <b class="${move(v.portfolio_pct)}">${spct(v.portfolio_pct)}</b></span>
      <span>SPY <b class="${move(v.spy_pct)}">${spct(v.spy_pct)}</b></span>
      <span>QQQ <b class="${move(v.qqq_pct)}">${spct(v.qqq_pct)}</b></span>
      <span class="cc-dimtext">vs SPY</span> <b>${pts(v.diff_vs_spy_pp)}</b> <span class="cc-dimtext">· vs QQQ</span> <b>${pts(v.diff_vs_qqq_pp)}</b></span>`;
  }

  // balanced desktop rows: at most 4 per row and rows as even as possible (8 -> 4 + 4, 6 -> 3 + 3, 7 -> 4 + 3)
  const balanced = (n, max) => { const rows = Math.max(1, Math.ceil(n / (max || 4))); return Math.max(1, Math.ceil(n / rows)); };

  // 2.8D card: CURRENT VIEW (the unchanged 2.7G state from this same response) leads; the 2.7F attention level is a
  // small secondary badge. When no state was returned the card says so — the attention level never stands in for it.
  function stockCard(c, st) {
    const r = c.research, SR = window.StockResult;
    const hasResearch = r.available && r.freshness !== "MISSING";
    const current = hasResearch && (r.freshness === "FRESH" || r.freshness === "AGING");       // existing freshness labels
    const research = hasResearch
      ? `${tag(cap(r.view), "info")} ${SR ? SR.freshTag(r.freshness, r.age_hours) : fresh(r.freshness, r.age_hours != null ? `${r.age_hours}h` : "")}`
      : st && st.state === "RESEARCH NEEDED" ? `<span class="cc-dimtext">None saved</span>`     // the view already says it
        : SR ? SR.freshTag("MISSING") : `<span class="cc-dimtext">No saved research</span>`;
    const view = st && SR ? SR.stateTag(st.state) : `<span class="cc-dimtext">Current view unavailable</span>`;
    // one stock-specific note; portfolio-wide facts (sector exposure) live in the hero/attention, research in its cell
    const generic = (x) => / exposure is high$/.test(x) || /^research (needed|is stale)$/.test(x);
    const reasons = c.attention.reasons.filter((x) => !generic(x));
    if (c.price_location === "NEAR_RESISTANCE" && !reasons.includes("near resistance")) reasons.push("near resistance");
    if (c.extended && !reasons.includes("moved up quickly")) reasons.push("moved up quickly");
    const research_left = c.attention.reasons.filter((x) => /^research (needed|is stale)$/.test(x));
    const lvl = c.attention.level;
    return `<div class="cc-stock cc-stock-compact cc-dcard" data-sym="${esc(c.symbol)}" title="${esc(c.sentence)}">
      <div class="cc-stock-head"><strong class="cc-sym">${esc(c.symbol)}</strong>
        ${lvl && lvl !== "Normal" ? `<span class="cc-att" title="Portfolio attention level (${esc(c.attention.reasons.join(", "))})"><span class="cc-dot cc-${ATT[lvl] || "dim"}"></span>${esc(lvl)}</span>` : ""}
        <span class="cc-mv ${move(c.session_pct)}">${spct(c.session_pct)}</span></div>
      <div class="cc-line cc-state-line" title="Current view from today's rules (same as the Daily Review)"><span class="cc-label">CURRENT VIEW</span> ${view}</div>
      <div class="cc-mini3">
        <div><span class="cc-label">P&amp;L</span><b class="${move(c.result)}">${signed(c.result)}</b><span class="cc-small ${move(c.result)}">${spct(c.result_pct)}</span></div>
        <div><span class="cc-label">WEIGHT</span><b>${pct(c.weight)}</b><span class="cc-small cc-dimtext">of account</span></div>
        <div><span class="cc-label">RESEARCH</span><span class="cc-research">${research}</span></div></div>
      ${reasons.length ? `<div class="cc-line cc-why" data-reasons="${esc(JSON.stringify(reasons.concat(research_left)))}">⚠ ${esc(sentenceCase(reasons[0]))}</div>` : ""}
      <div class="cc-actions"><button class="cc-btn cc-mini${current ? "" : " cc-primary cc-analyze-needed"}" data-analyze="${esc(c.symbol)}">Analyze</button>
        <button class="cc-btn cc-mini${current ? " cc-primary" : ""}" data-quick="${esc(c.symbol)}">Quick check</button>
        <button class="cc-btn cc-mini cc-quiet" data-review="${esc(c.symbol)}">Review</button></div>
      <div class="cc-confirm-host" data-result-for="${esc(c.symbol)}"></div></div>`;
  }

  function myStocks(d) {
    if (!d.my_stocks) return "";
    const cov = d.research_coverage;
    const need = cov.needs_research.length;
    return `<section class="cc-card cc-compact cc-mystocks"><div class="cc-head"><h2>MY STOCKS TODAY <span class="cc-small cc-dimtext">(${d.my_stocks.length})</span></h2>
        <div class="cc-head-tools">${vsLine(d)} ${toggle("cc-vs-details", "Why is it different?")}
        ${need ? `<button class="cc-btn cc-mini cc-primary" id="cc-analyze-all">Analyze my holdings</button>` : tag("Research is fresh for every holding", "ok")}</div></div>
      <div id="cc-analyze-host"></div>
      ${batchResult && window.ResearchBatch ? (window.ResearchBatch.statusHtml ? window.ResearchBatch.statusHtml(batchResult) : window.ResearchBatch.resultHtml(batchResult)) : ""}
      ${d.events_note ? `<div class="cc-banner cc-warn">${esc(d.events_note)}</div>` : ""}
      <div class="cc-stocks" style="--cc-cols:${balanced(d.my_stocks.length)}">${d.my_stocks.map((c) => stockCard(c, (d.decision_states || {})[c.symbol])).join("")}</div>
      <div class="cc-panel" id="cc-vs-details" hidden>${vsMarket(d)}</div>
      <div class="cc-foot"><span class="cc-note">${esc(cov.note)} Hover a card for its one-line summary.</span>
        ${d.layers && d.layers.length ? toggle("cc-layers-panel", "Market → stock → portfolio") : ""}</div>
      ${layers(d)}</section>`;
  }

  // The 2.7F per-holding layer view — unchanged content, now an on-demand panel inside My Stocks (no request).
  function layers(d) {
    if (!d.layers || !d.layers.length) return "";
    const opts = d.layers.map((l, i) => `<option value="${i}">${esc(l.symbol)}</option>`).join("");
    return `<div class="cc-panel" id="cc-layers-panel" hidden><div class="cc-head"><h3>MARKET → STOCK → PORTFOLIO</h3><label class="cc-small">Stock <select id="cc-layer-sel">${opts}</select></label></div>
      <div id="cc-layer-body">${layerBody(d.layers[0])}</div></div>`;
  }
  function layerBody(l) {
    const what = ["", " — trend & research", " — attention level"];
    const word = (c, i) => (i === 0 && c.value === "Cautious" ? "Caution" : c.value);        // market: one word on every screen
    return `<div class="cc-layers">${l.cards.map((c, i) => `<div class="cc-layer cc-${LEVEL[c.level] || "dim"}"><div class="cc-label">${i + 1} · ${esc(c.layer)}${what[i] || ""}</div>
      <div class="cc-vval">${esc(word(c, i))}</div><div class="cc-small">${esc(T(c.text))}</div></div>${i < 2 ? `<div class="cc-arrow" aria-hidden="true">→</div>` : ""}`).join("")}</div>
      <div class="cc-fit"><div class="cc-label">HOW THESE FIT TOGETHER</div><p>${esc(l.how_these_fit)}</p></div>`;
  }

  // ---- WATCHLIST — names you do NOT own are the candidates; owned names are a small summary (they are in My Stocks)
  function watchCard(x, ctx) {
    const r = ctx && ctx.research, SR = window.StockResult;
    const own = x.owned === false ? tag("Not owned", "dim") : `<span title="Robinhood is not connected">${tag("Ownership unknown", "dim")}</span>`;
    const research = !r ? `<span class="cc-dimtext">Unavailable</span>` : r.available && r.freshness !== "MISSING"
      ? `${tag(cap(r.view), "info")} ${SR ? SR.freshTag(r.freshness, r.age_hours) : esc(r.freshness)}` : SR ? SR.freshTag("MISSING") : `<span class="cc-dimtext">Research needed</span>`;
    // a watchlist state needs event data this page does not load; Analyze shows it (the card then updates in place)
    const hint = "Check setup";
    return `<div class="cc-watch-row cc-wcard"><div class="cc-watch-item" data-sym="${esc(x.symbol)}">
        <div class="cc-stock-head"><strong class="cc-sym">${esc(x.symbol)}</strong>${own}<span class="cc-mv ${move(x.session_pct)}">${spct(x.session_pct)}</span></div>
        <div class="cc-line cc-state-line" title="The current view appears after Analyze — watchlist states need event data this page does not load"><span class="cc-label">CURRENT VIEW</span> <span class="cc-dimtext">${hint}</span></div>
        <div class="cc-mini3 cc-wrow"><div><span class="cc-label">PRICE · TREND</span><b>${money(x.price)}</b><span class="cc-small cc-dimtext">Trend ${esc(arrow(ctx && ctx.trend))} ${esc((ctx && ctx.trend) || "N/A")}</span></div>
          <div><span class="cc-label">RESEARCH</span><span class="cc-research">${research}</span></div>
          <div class="cc-actions"><button class="cc-btn cc-mini${r && r.available && r.freshness !== "MISSING" ? " cc-primary" : ""}" data-quick="${esc(x.symbol)}">Check</button>
            <button class="cc-btn cc-mini${r && r.available && r.freshness !== "MISSING" ? "" : " cc-primary"}" data-analyze-w="${esc(x.symbol)}">Analyze</button></div></div>
        <div class="cc-line cc-wait" hidden></div></div>
      <div class="cc-confirm-host" data-result-for="${esc(x.symbol)}"></div></div>`;
  }

  function watchlist(d) {
    const w = d.watchlist || [], ctx = d.watchlist_context || {};
    if (!w.length) return "";
    const known = d.robinhood.connected;
    const cands = known ? w.filter((x) => x.owned === false) : w;      // offline: ownership unknown, never guessed
    const owned = known ? w.filter((x) => x.owned === true) : [];
    const cols = cands.length <= 4 ? 4 : balanced(cands.length, 5);
    const strip = owned.length ? `<div class="cc-wowned" title="Already in My Stocks — click one to jump to its card"><span class="cc-label">ALSO WATCHED · OWNED</span>
        ${owned.map((x) => `<button class="cc-ochip" data-goto="${esc(x.symbol)}">${esc(x.symbol)} <span class="${move(x.session_pct)}">${spct(x.session_pct)}</span></button>`).join("")}</div>` : "";
    return `<section class="cc-card cc-compact cc-watchlist"><div class="cc-head"><h2 title="${known ? "New candidates you follow — being on the watchlist does not mean you own them" : ""}">${known ? `WATCHLIST — NOT OWNED <span class="cc-small cc-dimtext">(${cands.length})</span>` : "MY WATCHLIST"}</h2>
        <div class="cc-head-tools">${known ? strip : `<span class="cc-small cc-dimtext">Robinhood is not connected, so ownership is unknown</span>`}</div></div>
      <div class="cc-watch cc-wgrid" style="--cc-cols:${cols}">${cands.map((x) => watchCard(x, ctx[x.symbol])).join("")}</div>
      ${known && !cands.length ? `<p class="cc-small cc-dimtext">Every watchlist name is already in My Stocks.</p>` : ""}</section>`;
  }

  // ---- OPPORTUNITIES TO RESEARCH — one compact form row; results grouped side by side ------------------------------
  function opportunities(d) {
    const cash = d.robinhood.connected ? d.robinhood.summary.cash : null;
    return `<section class="cc-card cc-compact cc-opps"><div class="cc-head"><h2>OPPORTUNITIES TO RESEARCH</h2>
        ${d.robinhood.connected ? `<div class="cc-head-tools cc-form cc-nm-form"><label>Amount $ <input id="cc-nm-amt" type="number" min="1" step="1" value="500" /></label>
          <label><input type="radio" name="cc-nm-fund" value="new_money" checked /> New money</label>
          <label><input type="radio" name="cc-nm-fund" value="cash" /> Use current cash (${money(cash)})</label>
          <button class="cc-btn cc-mini cc-primary" id="cc-nm-run">Show where to research</button></div>`
          : `<div class="cc-head-tools"><span class="cc-small cc-dimtext">Connect Robinhood to see how new money would affect your portfolio.</span></div>`}</div>
      <p class="cc-small cc-dimtext cc-nm-note">Groups your holdings and watchlist by current conditions, so you know where to research first. It is not a ranking and never picks a "best" stock.</p>
      <div id="cc-nm-out"></div>
      <details class="cc-more" id="cc-scanner"><summary>Most active stocks today (scanner — activity, not quality)</summary><div class="cc-more-body">Loading…</div></details></section>`;
  }

  function candidate(c) {
    const chase = c.price_location === "EXTENDED";
    const lists = c.supports.map((x) => `✓ ${T(x)}`).concat(c.patience.map((x) => `⚠ ${T(x)}`)).slice(0, 4).join("\n");
    const note = c.patience[0] ? `<div class="cc-small cc-li-warn">${esc(T(c.patience[0]))}</div>` : c.supports[0] ? `<div class="cc-small cc-li-ok">${esc(T(c.supports[0]))}</div>` : "";
    return `<div class="cc-cand cc-cand-compact" title="${esc(lists)}"><div class="cc-stock-head"><strong class="cc-sym">${esc(c.symbol)}</strong>${c.owned ? tag("Owned", "info") : tag("Watchlist", "dim")}
        <span class="cc-small">${c.research_needed ? tag("RESEARCH NEEDED", "dim") : `${tag(cap(c.research_view), "info")} ${fresh(c.research_freshness)}`}</span>
        <button class="cc-btn cc-mini" data-quick="${esc(c.symbol)}">Check</button></div>
      <div class="cc-small">Trend ${esc(arrow(c.trend))} ${esc(TREND[c.trend] || "N/A")} · ${chase ? tag("Extended — chasing risk", "warn") : esc(LOC[c.price_location] || c.price_location)} · Event risk ${esc(c.event_risk)}
        · Weight ${pct(c.weight_before)} → ${pct(c.weight_after)}${c.sector !== "UNCLASSIFIED" ? ` · ${esc(c.sector)} ${pct(c.sector_before)} → ${pct(c.sector_after)}` : ""}</div>
      ${note}</div>`;
  }

  async function runNewMoney(root) {
    const out = root.querySelector("#cc-nm-out");
    const amount = Number(root.querySelector("#cc-nm-amt").value);
    const funding = root.querySelector('input[name="cc-nm-fund"]:checked').value;
    out.innerHTML = "<p class='hint'>Checking current conditions for your holdings and watchlist (no AI calls)…</p>";
    const r = await post("/api/insights/new-money", { amount_usd: amount, funding, analysis_ids: ResearchIds.all() }).catch(() => null);
    if (!r || !r.groups) { out.innerHTML = `<div class="cc-banner cc-warn">${esc((r && (r.message || r.detail)) || "Could not check right now.")}</div>`; return; }
    const G = { "CONDITIONS WORTH RESEARCHING": "ok", "MIXED — NEED MORE CONFIRMATION": "warn", "HIGHER-RISK CONDITIONS": "alert" };
    const groups = Object.entries(r.groups), full = groups.filter(([, l]) => l.length), empty = groups.filter(([, l]) => !l.length);
    const none = empty.length ? `<span class="cc-small cc-dimtext">None right now: ${empty.map(([g]) => esc(cap(g))).join(" · ")}</span>` : "";
    // groups side by side (alphabetical inside each, as returned); a single group spreads across the width instead
    const body = full.length === 1
      ? `<div class="cc-ngroup-line">${tag(full[0][0], G[full[0][0]] || "info")} <span class="cc-small">All ${full[0][1].length} names are in this group.</span> ${none}</div>
         <div class="cc-cgrid" style="--cc-cols:${balanced(full[0][1].length)}">${full[0][1].map(candidate).join("")}</div>`
      : full.length ? `<div class="cc-ngroups" style="--cc-cols:${full.length}">${full.map(([g, list]) => `<div class="cc-group cc-ngroup cc-${G[g]}"><h4>${esc(g)} <span class="cc-small">(${list.length})</span></h4>
          ${list.map(candidate).join("")}</div>`).join("")}</div>${none ? `<div class="cc-ngroup-line">${none}</div>` : ""}`
        : `<p class="cc-small cc-dimtext">No holdings or watchlist names to group right now.</p>`;
    out.innerHTML = `${body}<p class="cc-note">${esc(r.note)}${r.skipped.length ? ` Skipped: ${r.skipped.map((s) => `${esc(s.symbol)} (${esc(s.reason)})`).join(", ")}.` : ""}</p>`;
    wireQuick(out);
  }

  // ---- LOWER WORKSPACE: RISKS · EVENTS · WATCH NEXT · TRADING PATTERN (existing observations only) ------------------
  // What to watch next as short chips: the same backend sentences, shortened; anything unrecognised is shown as-is.
  function watchChip(t, d) {
    let x;
    if ((x = /^The (.+) on (\d{4}-\d\d-\d\d)\.$/.exec(t))) return null;                      // shown under EVENTS
    if ((x = /^Whether more stocks start rising \(currently ([\d.]+)% of the breadth list advancing\)/.exec(t))) return ["Breadth", `${x[1]}% rising`];
    if ((x = /^Whether volatility cools \((\S+) 20-day volatility ([\d.]+)%\)/.exec(t))) return ["Volatility", `${x[1]} 20-day ${x[2]}%`];
    if ((x = /^Whether (\S+)'s 5-day momentum turns positive \(currently (.+)\)/.exec(t))) return [`${x[1]} 5-day momentum`, x[2]];
    if ((x = /^Whether (\S+) holds its recent gains \(5-day (.+)\)/.exec(t))) return [`${x[1]} recent gains`, `5-day ${x[2]}`];
    if ((x = /^(.+) trend \((\S+)\), since it is ([\d.]+)% of your portfolio/.exec(t))) {
      const tile = (d.market.tiles || []).find((k) => k.symbol === x[2]);
      return [T(`${x[1]} trend`) + ` (${x[2]})`, tile && tile.available ? `${spct(tile.pct)} ${sess()}` : `${x[3]}% of your portfolio`];
    }
    return [T(t.length > 60 ? `${t.slice(0, 58)}…` : t), ""];
  }

  function lower(d) {
    const m = d.market, states = d.decision_states || {}, cards = d.my_stocks || [];
    const SR = window.StockResult, goto = (s) => `<button class="cc-ochip" data-goto="${esc(s)}">${esc(s)}</button>`;
    // RISKS — holdings in the unchanged 2.7G "needs attention" group, one row per identical state (same meaning), and
    // which holdings lack current research
    let risk;
    if (!d.robinhood.connected) risk = `<p class="cc-small cc-dimtext">Robinhood is not connected — holding risks are unknown.</p>`;
    else {
      const attn = cards.filter((c) => states[c.symbol] && states[c.symbol].group === "NEEDS ATTENTION").map((c) => c.symbol).sort();
      const byState = {};
      attn.forEach((s) => { (byState[states[s].state] = byState[states[s].state] || []).push(s); });
      const noRes = cards.filter((c) => c.research.freshness === "MISSING" || c.research.freshness === "STALE").map((c) => c.symbol).sort();
      const aging = cards.filter((c) => c.research.freshness === "AGING").map((c) => c.symbol).sort();
      risk = `${d.decision_states ? `<div class="cc-lrow"><span class="cc-label">NEEDS ATTENTION (CURRENT VIEW)</span>
          ${attn.length ? Object.entries(byState).map(([st, syms]) => `<div class="cc-lline">${SR ? SR.stateTag(st) : esc(st)} ${syms.map(goto).join("")}</div>`).join("")
            : `<div class="cc-small cc-dimtext">No holding is in a needs-attention state.</div>`}</div>`
          : `<div class="cc-small cc-dimtext">Current views unavailable.</div>`}
        <div class="cc-lrow"><span class="cc-label">RESEARCH</span>
          ${noRes.length ? `<div class="cc-lline"><span class="cc-small">Missing or stale</span> ${noRes.map(goto).join("")}</div>` : `<div class="cc-small">Every holding has current research.</div>`}
          ${aging.length ? `<div class="cc-small cc-dimtext">Aging: ${aging.map(esc).join(" · ")}</div>` : ""}</div>`;
    }
    // EVENTS — verified market events, one short row each; explanations behind Details; loading/unavailable say so
    const evs = m.available ? (m.events || []) : [];
    const held = cards.filter((c) => c.attention.reasons.some((x) => /event/.test(x))).map((c) => c.symbol);
    const events = `${d.events_note ? `<div class="cc-small cc-warn-text">Events loading — event risk for holdings is unknown for now.</div>` : ""}
      ${!m.available ? `<div class="cc-small cc-dimtext">Event data unavailable.</div>`
        : evs.length ? evs.map((e) => `<div class="cc-lline cc-ev"><b>${esc(eventName(e.title))}</b> <span class="cc-small cc-dimtext">${esc(shortDate(e.date))} · ${esc(dayz(e.days_until))}</span></div>`).join("")
          : `<div class="cc-small cc-dimtext">No verified major market event in the next few days.</div>`}
      ${held.length ? `<div class="cc-small">Holdings with an event coming up: ${held.map(esc).join(" · ")}</div>` : ""}
      ${evs.length ? `<div class="cc-panel" id="cc-events-more" hidden>${evs.map((e) => `<p class="cc-small"><b>${esc(eventName(e.title))}:</b> ${esc(e.why_it_matters || "")}</p>`).join("")}</div>` : ""}`;
    // WATCH NEXT — at most 4 short rows; events are already listed next to it
    const chips = ((m.available && m.what_to_watch) || []).map((t) => watchChip(t, d)).filter(Boolean).slice(0, 4);
    const watch = chips.length ? chips.map(([t, sub]) => `<div class="cc-wchip"><b>${esc(t)}</b>${sub ? ` <span class="cc-small cc-dimtext">${esc(sub)}</span>` : ""}</div>`).join("")
      : `<div class="cc-small cc-dimtext">Nothing specific right now.</div>`;
    // TRADING PATTERN — verified behavioural history; context only, never evidence
    const p = d.pattern;
    const pattern = p ? `<div class="cc-lcol cc-patterncard"><h3 class="cc-label">TRADING PATTERN</h3>
        <div class="cc-pline"><span class="cc-pnum">${esc(p.count)} <span>/ ${esc(p.of)}</span></span> <b>Entries after a fast rise</b></div>
        <div class="cc-small cc-dimtext">Context only · not a prediction · never changes a view</div>
        <div class="cc-lbtns"><button class="cc-btn cc-mini" id="cc-pattern-review">Review pattern</button>${toggle("cc-pattern-more", "More", true)}</div>
        <div class="cc-panel" id="cc-pattern-more" hidden><p class="cc-small">${esc(p.text)} ${esc(p.explanation)}</p>
          <p class="cc-small"><strong>Don't chase:</strong> after a fast rise it is harder to tell where the idea would be wrong. A pullback toward support or a pause with confirmation usually gives a clearer entry to evaluate.</p>
          <div class="cc-small cc-dimtext">${esc(p.source)}</div></div></div>` : "";
    // natural-height columns: RISKS | EVENTS over WATCH NEXT | TRADING PATTERN
    return `<section class="cc-card cc-compact cc-lower"><div class="cc-head"><h2>RISKS · EVENTS · WATCH NEXT</h2></div>
      <div class="cc-lower-grid" style="--cc-cols:${p ? 3 : 2}">
        <div class="cc-lcol"><h3 class="cc-label">RISKS</h3>${risk}</div>
        <div class="cc-lstack"><div class="cc-lcol"><div class="cc-lhead"><h3 class="cc-label">EVENTS</h3>${evs.length ? toggle("cc-events-more", "Details", true) : ""}</div>${events}</div>
          <div class="cc-lcol"><h3 class="cc-label">WATCH NEXT</h3>${watch}</div></div>
        ${pattern}</div>
      <p class="cc-note">Descriptions of current conditions — not predictions and not buy/sell instructions. This app never places orders.</p></section>`;
  }

  // ---- wiring --------------------------------------------------------------------------------------------------
  function wireQuick(scope) {
    scope.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => window.TraderReview && window.TraderReview.quick(b.dataset.quick, 500)));
  }

  // 2.7G.3: the result stays visible under the card; only this card's research line is refreshed (no full reload).
  function analyzeOne(sym, host, root) {
    if (busy || !window.StockResult) return;
    window.StockResult.analyze(sym, host, { onResearch: (s, r, dec) => {
      root.querySelectorAll(`.cc-watch-item[data-sym="${s}"]`).forEach((item) => {           // 2.8D watchlist card
        const slot = item.querySelector(".cc-research");
        if (slot && r.available) slot.innerHTML = `${tag(cap(r.research_view), "info")} ${window.StockResult.freshTag(r.freshness, r.age_hours)}`;
        const st = item.querySelector(".cc-state-line"), wait = item.querySelector(".cc-wait");
        if (st && dec) st.innerHTML = `<span class="cc-label">CURRENT VIEW</span> ${window.StockResult.stateTag(dec.state)}`;
        if (wait && dec && dec.waiting_for && dec.waiting_for.length) { wait.textContent = `Waiting for: ${dec.waiting_for[0]}`; wait.hidden = false; }
      });
      root.querySelectorAll(`.cc-stock[data-sym="${s}"]`).forEach((card) => {
        const slot = card.querySelector(".cc-research");
        if (slot && r.available) slot.innerHTML = `${tag(cap(r.research_view), "info")} ${window.StockResult.freshTag(r.freshness, r.age_hours)}`;
        const st = card.querySelector(".cc-state-line");
        if (st && dec) st.innerHTML = `<span class="cc-label">CURRENT VIEW</span> ${window.StockResult.stateTag(dec.state)}`;
        const btn = card.querySelector("[data-analyze]"), qb = card.querySelector("[data-quick]");
        if (btn && r.available && (r.freshness === "FRESH" || r.freshness === "AGING")) {
          btn.classList.remove("cc-analyze-needed", "cc-primary"); if (qb) qb.classList.add("cc-primary");
        }
        // the card's "research needed" caution is now out of date: show the next one instead (no recalculation here)
        const why = card.querySelector(".cc-why");
        if (why && r.available) {
          const left = JSON.parse(why.dataset.reasons || "[]").filter((x) => !/^research (needed|is stale)$/.test(x));
          if (left.length) why.textContent = `⚠ ${sentenceCase(left[0])}`; else why.remove();
        }
        const t = `${s}'s research was updated just now — see the current view below.`;
        if (card.classList.contains("cc-expanded")) card.dataset.title = t; else card.title = t;   // no tooltip over the open panel
      });
    } });
  }

  // 2.7G.2: same limit-aware plan as the 2.7G Daily Review (planner + limits + AI cache live server-side).
  async function analyzeAll(root) {
    const host = root.querySelector("#cc-analyze-host");
    if (busy || !window.ResearchBatch) return;
    host.innerHTML = `<p class="hint">Checking research coverage and AI capacity (no AI calls)…</p>`;
    const cov = await window.ResearchBatch.coverage(ResearchIds.all()).catch(() => null);
    if (!cov) { host.innerHTML = `<div class="cc-banner cc-warn">Research coverage is unavailable right now (is Robinhood connected?). No AI calls were made.</div>`; return; }
    host.innerHTML = window.ResearchBatch.confirmHtml(cov);
    host.querySelector("[data-no]").addEventListener("click", () => { host.innerHTML = ""; });
    const yes = host.querySelector("[data-yes]");
    if (!yes) return;
    yes.addEventListener("click", async () => {                 // explicit confirmation; limits re-checked inside run()
      busy = true;
      batchResult = await window.ResearchBatch.run(cov.need, (sym, i, n) => {
        host.innerHTML = `<p class="hint">Researching ${esc(sym)} (${i} of ${n})…</p>`;
      }).catch((e) => ({ updated: [], still_needed: cov.need, reason: `Error (${e.message})` }));
      batchResult.coverage = cov;             // 2.8E: the status line and its Details reuse the numbers shown before the run
      busy = false;
      render(root, false);
    });
  }

  async function loadScanner(box) {
    const ov = await getJSON("/api/market/overview").catch(() => null);
    if (!ov || !ov.top_gainers) { box.innerHTML = "<p class='cc-small'>Scanner unavailable.</p>"; return; }
    const seen = new Map();
    ["top_gainers", "top_losers", "unusual_volume", "momentum_stocks", "possible_breakouts"].forEach((k) => (ov[k] || []).forEach((x) => {
      if (!seen.has(x.symbol) || seen.get(x.symbol).attention_score < x.attention_score) seen.set(x.symbol, x);
    }));
    const top = [...seen.values()].sort((a, b) => b.attention_score - a.attention_score).slice(0, 6);
    box.innerHTML = `<table class="data-table"><tr><th>Stock</th><th class="pf-num">Today</th><th>Activity</th></tr>
      ${top.map((x) => `<tr class="mk-row" data-sym="${esc(x.symbol)}"><td><strong>${esc(x.symbol)}</strong></td><td class="pf-num ${move(x.pct_change)}">${spct(x.pct_change)}</td><td>${esc(x.signal)}</td></tr>`).join("")}</table>
      <p class="hint">Unusual activity only — not recommendations. Click a row to open its research.</p>`;
    box.querySelectorAll(".mk-row").forEach((r) => r.addEventListener("click", () => { if (window.openDetail) window.openDetail(r.dataset.sym); }));
  }

  function wire(root) {
    root.querySelectorAll("[data-cc-refresh]").forEach((b) => b.addEventListener("click", () => render(root, true)));
    const eb = root.querySelector("#cc-explain-btn");
    if (eb && window.MarketContext) eb.addEventListener("click", () => window.MarketContext.explainInto(root.querySelector("#mk-explain")));
    // 2.8A/B: details, news and explanations are hidden panels opened on demand (no request unless noted)
    root.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", async () => {
      const panel = root.querySelector(`#${b.dataset.toggle}`);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      b.setAttribute("aria-expanded", String(!panel.hidden));
      const lazy = panel.querySelector('[data-lazy="market"]');
      if (!panel.hidden && lazy && !lazy.dataset.loaded && window.MarketContext) {   // same loader as before (cached market)
        lazy.dataset.loaded = "1";
        const m = await window.MarketContext.load(false).catch(() => null);
        lazy.innerHTML = m ? window.MarketContext.drivers(m) + window.MarketContext.events(m) : "<p class='cc-small'>Unavailable.</p>";
      }
    }));
    const sc = root.querySelector("#cc-scanner");
    if (sc) sc.addEventListener("toggle", () => { if (sc.open && !sc.dataset.loaded) { sc.dataset.loaded = "1"; loadScanner(sc.querySelector(".cc-more-body")); } });
    root.querySelectorAll(".cc-i").forEach((b) => b.addEventListener("click", () => b.nextElementSibling.classList.toggle("cc-tip-open")));
    const vp = root.querySelector("#cc-view-portfolio");
    if (vp) vp.addEventListener("click", () => { const t = document.querySelector('.tab-btn[data-tab="portfolio"]'); if (t) t.click(); });
    root.querySelectorAll("[data-analyze]").forEach((b) => b.addEventListener("click", () => analyzeOne(b.dataset.analyze, b.closest(".cc-stock").querySelector(".cc-confirm-host"), root)));
    root.querySelectorAll("[data-analyze-w]").forEach((b) => b.addEventListener("click", () => analyzeOne(b.dataset.analyzeW, b.closest(".cc-watch-row").querySelector(".cc-confirm-host"), root)));
    root.querySelectorAll("[data-review]").forEach((b) => b.addEventListener("click", () => window.TraderReview && window.TraderReview.openPosition(b.dataset.review)));
    const all = root.querySelector("#cc-analyze-all");
    if (all) all.addEventListener("click", () => analyzeAll(root));
    const sel = root.querySelector("#cc-layer-sel");
    if (sel) sel.addEventListener("change", () => { root.querySelector("#cc-layer-body").innerHTML = layerBody(last.layers[Number(sel.value)]); });
    const nm = root.querySelector("#cc-nm-run");
    if (nm) nm.addEventListener("click", () => runNewMoney(root));
    // 2.8D: owned-watchlist / risk chips jump to the holding's card; "Review pattern" opens Trader Review (no request)
    root.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => {
      const card = root.querySelector(`.cc-stock[data-sym="${b.dataset.goto}"]`);
      if (!card) return;
      card.scrollIntoView({ block: "center" });
      card.classList.remove("cc-flash"); void card.offsetWidth; card.classList.add("cc-flash");
    }));
    const pr = root.querySelector("#cc-pattern-review");
    if (pr) pr.addEventListener("click", () => window.TraderReview && window.TraderReview.patterns && window.TraderReview.patterns());
    wireQuick(root);
  }

  async function render(root, refresh) {
    if (!root) return;
    if (!last) root.innerHTML = `<p class="hint">Loading your command center…</p>`;
    try {
      if (refresh) await getJSON("/api/insights/market?refresh=true").catch(() => null);
      const d = await post("/api/insights/home", { analysis_ids: ResearchIds.all() });
      if (!d || !d.market) throw new Error((d && d.detail) || "unexpected response");
      last = d;
      root.innerHTML = `<div class="cc">${hero(d.market)}${robinhood(d)}${attention(d)}${myStocks(d)}${watchlist(d)}${opportunities(d)}${lower(d)}</div>`;
      wire(root);
      if (window.StockResult) window.StockResult.reopen(root);
    } catch (e) {
      root.innerHTML = `<div class="cc-banner cc-warn">The command center could not load (${esc(e.message)}). If the server was started before this update, restart it. The Advanced dashboard still works.</div>`;
    }
  }

  window.CommandCenter = { render, get last() { return last; } };
})();
