// app.js — Stock Agent Dashboard frontend.
//
// Plain fetch()-polling against the FastAPI backend under /api/*. No build
// step, no framework — a stand-in for the React/TypeScript dashboard until
// Node.js is set up (see project plan). Keep this in sync with
// config.WEB_UI_REFRESH_SECONDS on the Python side if you change the cadence.
const REFRESH_MS = 30000;

const fmt = {
  price: (v) => (v == null ? "N/A" : `$${Number(v).toFixed(2)}`),
  pct: (v) => (v == null ? "N/A" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`),
  num: (v, d = 2) => (v == null ? "N/A" : Number(v).toFixed(d)),
  rvol: (v) => (v == null ? "N/A" : `${Number(v).toFixed(2)}x`),
  score: (v) => (v == null ? "N/A" : `${v}/100`),
};

function pctClass(v) {
  if (v == null) return "";
  return v >= 0 ? "pct-up" : "pct-down";
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    // Research History is data-heavy and rarely changes moment-to-moment, so it
    // loads on demand (tab open / explicit refresh) rather than every poll cycle.
    if (btn.dataset.tab === "history") {
      refreshHistory();
      refreshPerformance();
    }
  });
});

// ---------------------------------------------------------------------------
// Status pill (health check)
// ---------------------------------------------------------------------------
async function refreshStatus() {
  const pill = document.getElementById("status-pill");
  try {
    const health = await api("/api/health");
    if (!health.alpaca_configured) {
      pill.textContent = "Alpaca API key not configured — add it to .env";
      pill.className = "status-pill error";
    } else {
      pill.textContent = health.llm_configured ? "Live · AI enabled" : "Live · AI analysis unavailable (no LLM key)";
      pill.className = "status-pill ok";
    }
  } catch (err) {
    pill.textContent = "Backend unreachable";
    pill.className = "status-pill error";
  }
}

// ---------------------------------------------------------------------------
// AI usage (agents/usage_tracker.py)
// ---------------------------------------------------------------------------
async function refreshAIUsage() {
  const pill = document.getElementById("ai-usage-pill");
  const detail = document.getElementById("ai-usage-detail");
  try {
    const usage = await api("/api/ai/usage");
    // Stage 3.9: two independent budgets. Research = every AI feature before Stage 3.8; Explain = Strategy Fit / Evidence.
    const r = (usage.budgets || {}).research, x = (usage.budgets || {}).explanation;
    if (!r || !x) {
      pill.textContent = `AI usage: ${usage.calls}/${usage.daily_call_limit} calls today`;
      detail.textContent = `Calls: ${usage.calls} · Daily limit: ${usage.daily_call_limit}`;
      return;
    }
    const line = (b) => `${b.used_today}/${b.daily_limit} today · ${b.used_last_hour}/${b.hourly_limit} this hour`;
    pill.textContent = `AI · Research ${r.used_today}/${r.daily_limit} · Explain ${x.used_today}/${x.daily_limit}`;
    pill.title = `AI calls today — separate budgets\nResearch: ${line(r)}\nExplain (Strategy Fit / Evidence): ${line(x)}`;
    detail.innerHTML = `
      <strong>Research</strong> ${line(r)}${r.limit_reached ? " · <strong>limit reached</strong>" : ""} &middot;
      Tokens ${(r.input_tokens + r.output_tokens).toLocaleString()} &middot; Est. $${r.estimated_cost_usd.toFixed(4)}
      <br><strong>Explain</strong> ${line(x)}${x.limit_reached ? " · <strong>limit reached</strong>" : ""} &middot;
      Tokens ${(x.input_tokens + x.output_tokens).toLocaleString()} &middot; Est. $${x.estimated_cost_usd.toFixed(4)}
      <span class="muted">· one budget never reduces the other</span>
    `;
  } catch (err) {
    pill.textContent = "AI usage: unavailable";
    detail.textContent = `AI usage unavailable: ${err.message}`;
  }
}

// ---------------------------------------------------------------------------
// Market Overview
// ---------------------------------------------------------------------------
function renderTable(tableId, rows, columns) {
  const table = document.getElementById(tableId);
  if (!rows || rows.length === 0) {
    table.innerHTML = `<tr class="empty-row"><td>(none)</td></tr>`;
    return;
  }
  const header = `<tr>${columns.map((c) => `<th>${c.label}</th>`).join("")}</tr>`;
  const body = rows
    .map(
      (r) =>
        `<tr data-symbol="${r.symbol}">${columns.map((c) => `<td>${c.render(r)}</td>`).join("")}</tr>`
    )
    .join("");
  table.innerHTML = header + body;
  table.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.symbol));
  });
}

const moverColumns = [
  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },
  { label: "Change", render: (r) => `<span class="${pctClass(r.pct_change)}">${fmt.pct(r.pct_change)}</span>` },
  { label: "RVOL", render: (r) => fmt.rvol(r.relative_volume) },
  { label: "Signal", render: (r) => `<span class="signal-badge">${r.signal}</span>` },
];

const levelColumns = [
  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },
  { label: "Price", render: (r) => fmt.price(r.price) },
  { label: "Support", render: (r) => fmt.price(r.support) },
  { label: "Resistance", render: (r) => fmt.price(r.resistance) },
];

const volColumns = [
  { label: "Symbol", render: (r) => `<strong>${r.symbol}</strong>` },
  { label: "Ann. Volatility", render: (r) => `${fmt.num(r.volatility_pct)}%` },
  { label: "Vol. Expansion", render: (r) => fmt.rvol(r.volatility_expansion) },
];

function renderOpportunities(overview) {
  const seen = new Map();
  const all = [
    ...overview.top_gainers,
    ...overview.top_losers,
    ...overview.unusual_volume,
    ...overview.momentum_stocks,
    ...overview.possible_breakouts,
  ];
  all.forEach((m) => {
    if (!seen.has(m.symbol) || seen.get(m.symbol).attention_score < m.attention_score) {
      seen.set(m.symbol, m);
    }
  });
  const top = [...seen.values()].sort((a, b) => b.attention_score - a.attention_score).slice(0, 8);

  const grid = document.getElementById("ai-opportunities");
  if (top.length === 0) {
    grid.innerHTML = `<div class="hint">(none)</div>`;
    return;
  }
  grid.innerHTML = top
    .map(
      (m) => `
    <div class="card" data-symbol="${m.symbol}">
      <div class="card-symbol">${m.symbol}</div>
      <div class="card-price">${fmt.price(m.price)}</div>
      <div class="card-row"><span class="${pctClass(m.pct_change)}">${fmt.pct(m.pct_change)}</span><span>RVOL ${fmt.rvol(m.relative_volume)}</span></div>
      <div class="card-signal"><span class="signal-badge">${m.signal}</span></div>
      <div class="card-row"><span>Attention</span><span>${fmt.score(m.attention_score)}</span></div>
    </div>`
    )
    .join("");
  grid.querySelectorAll(".card").forEach((c) => c.addEventListener("click", () => openDetail(c.dataset.symbol)));
}

async function refreshOverview() {
  try {
    const overview = await api("/api/market/overview");
    document.getElementById("scan-meta").textContent = overview.used_fallback_universe
      ? `Alpaca screener unavailable — scanned a fixed fallback universe. ${overview.scanned_symbol_count} candidates, ${overview.liquid_symbol_count} passed liquidity filters.`
      : `${overview.scanned_symbol_count} candidates scanned, ${overview.liquid_symbol_count} passed liquidity filters.`;

    renderOpportunities(overview);
    renderTable("table-gainers", overview.top_gainers, moverColumns);
    renderTable("table-losers", overview.top_losers, moverColumns);
    renderTable("table-volume", overview.unusual_volume, moverColumns);
    renderTable("table-momentum", overview.momentum_stocks, moverColumns);
    renderTable("table-breakouts", overview.possible_breakouts, moverColumns);
    renderTable("table-support", overview.approaching_support, levelColumns);
    renderTable("table-resistance", overview.approaching_resistance, levelColumns);
    renderTable("table-volatility", overview.volatility_expansion, volColumns);
  } catch (err) {
    document.getElementById("scan-meta").textContent = `Market overview unavailable: ${err.message}`;
  }
}

// ---------------------------------------------------------------------------
// Watchlist
// ---------------------------------------------------------------------------
async function refreshWatchlist() {
  const grid = document.getElementById("watchlist-cards");
  try {
    const wl = await api("/api/watchlist");
    if (wl.symbols.length === 0) {
      grid.innerHTML = `<div class="hint">Watchlist is empty. Add a ticker above.</div>`;
      return;
    }
    grid.innerHTML = wl.symbols
      .map((sym) => {
        const m = wl.metrics[sym];
        if (!m) {
          return `<div class="card"><div class="card-symbol">${sym}</div><div class="hint">No data available</div></div>`;
        }
        return `
        <div class="card" data-symbol="${sym}">
          <button class="card-remove" data-remove="${sym}" title="Remove from watchlist">×</button>
          <div class="card-symbol">${sym}</div>
          <div class="card-price">${fmt.price(m.price)}</div>
          <div class="card-row"><span class="${pctClass(m.pct_change)}">${fmt.pct(m.pct_change)}</span><span>RVOL ${fmt.rvol(m.relative_volume)}</span></div>
          <div class="card-row"><span>RSI ${fmt.num(m.rsi, 1)}</span><span>Trend ${m.trend}</span></div>
          <div class="card-signal"><span class="signal-badge">${m.signal}</span></div>
          <div class="card-row"><span>Attention</span><span>${fmt.score(m.attention_score)}</span></div>
        </div>`;
      })
      .join("");

    grid.querySelectorAll(".card").forEach((c) => c.addEventListener("click", (e) => {
      if (e.target.dataset.remove) return;
      openDetail(c.dataset.symbol);
    }));
    grid.querySelectorAll("[data-remove]").forEach((btn) =>
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        await api(`/api/watchlist/${btn.dataset.remove}`, { method: "DELETE" });
        refreshWatchlist();
      })
    );
  } catch (err) {
    grid.innerHTML = `<div class="hint">Watchlist unavailable: ${err.message}</div>`;
  }
}

document.getElementById("add-symbol-btn").addEventListener("click", async () => {
  const input = document.getElementById("add-symbol-input");
  const msg = document.getElementById("add-symbol-msg");
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) return;
  try {
    await api("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    });
    input.value = "";
    msg.textContent = "";
    refreshWatchlist();
  } catch (err) {
    msg.textContent = err.message;
  }
});

// ---------------------------------------------------------------------------
// Stock detail modal (metrics + alternatives + lazy-loaded AI analysis)
// ---------------------------------------------------------------------------
const modal = document.getElementById("detail-modal");
document.getElementById("detail-close").addEventListener("click", () => modal.classList.add("hidden"));
modal.addEventListener("click", (e) => {
  if (e.target === modal) modal.classList.add("hidden");
});

function evidenceBarHtml(evidence) {
  return `
    <div class="evidence-legend">
      <span class="pct-up">Bullish ${evidence.bullish_pct}%</span>
      <span class="hint">Neutral ${evidence.neutral_pct}%</span>
      <span class="pct-down">Bearish ${evidence.bearish_pct}%</span>
    </div>
    <div class="evidence-bar">
      ${evidence.bullish_pct > 0 ? `<div class="seg bullish" style="width:${evidence.bullish_pct}%">${evidence.bullish_pct}%</div>` : ""}
      ${evidence.neutral_pct > 0 ? `<div class="seg neutral" style="width:${evidence.neutral_pct}%">${evidence.neutral_pct}%</div>` : ""}
      ${evidence.bearish_pct > 0 ? `<div class="seg bearish" style="width:${evidence.bearish_pct}%">${evidence.bearish_pct}%</div>` : ""}
    </div>
    <div class="hint">These percentages summarize current evidence. They are not probabilities of future returns.</div>
  `;
}

function viewBadgeClass(view) {
  if (view.includes("BULLISH")) return "bullish";
  if (view.includes("BEARISH")) return "bearish";
  return "mixed";
}

function eventRiskBadgeClass(level) {
  if (level === "HIGH") return "HIGH";
  if (level === "MEDIUM") return "MEDIUM";
  if (level === "LOW") return "LOW";
  return "NONE";
}

function eventLineHtml(e) {
  const when = e.time_precision === "EXACT" && e.event_datetime_utc
    ? new Date(e.event_datetime_utc).toLocaleString()
    : e.event_date + " (date only)";
  const tentative = e.confirmed ? "" : `<span class="hint"> &middot; tentative</span>`;
  const link = e.source_url ? `<a href="${e.source_url}" target="_blank" rel="noopener">${e.source}</a>` : e.source;
  return `<div class="event-item"><strong>${e.title}</strong> &mdash; ${when}${tentative}<div class="hint">Source: ${link}</div></div>`;
}

function eventRiskCardHtml(events) {
  if (!events) {
    return `<div class="narrative-section"><h4>Event Risk</h4><p class="hint">Event data unavailable for this symbol.</p></div>`;
  }
  const upcoming = [...events.company_events, ...events.macro_events]
    .filter((e) => e.status === "UPCOMING")
    .sort((a, b) => a.event_date.localeCompare(b.event_date))
    .slice(0, 3);
  const earningsLine = events.earnings_available
    ? ""
    : `<p class="hint">${events.earnings_reason || "Earnings-calendar data is not currently available from a verified provider."}</p>`;
  return `
    <div class="narrative-section">
      <h4>Event Risk <span class="event-risk-badge event-risk-${eventRiskBadgeClass(events.event_risk_level)}">${events.event_risk_level}</span></h4>
      ${upcoming.length ? upcoming.map(eventLineHtml).join("") : `<p class="hint">No known upcoming event within the tracked window.</p>`}
      ${earningsLine}
      <div class="hint">Severity is computed deterministically from real event dates — never a prediction of market direction.</div>
    </div>`;
}

function eventAdvancedDetailsHtml(events) {
  if (!events) return "";
  const all = [...events.company_events, ...events.macro_events];
  if (!all.length) return "";
  return `
    <h4 style="margin-top:14px;">Event details (advanced)</h4>
    ${all
      .map(
        (e) => `
      <div class="detail-metric-grid" style="margin-top:6px;">
        <div class="label">Event</div><div>${e.title} (${e.event_type})</div>
        <div class="label">Status</div><div>${e.status}${e.confirmed ? "" : " (tentative)"}</div>
        <div class="label">Time precision</div><div>${e.time_precision}</div>
        <div class="label">UTC timestamp</div><div>${e.event_datetime_utc ? new Date(e.event_datetime_utc).toISOString() : "N/A (date only)"}</div>
        <div class="label">Source provider</div><div>${e.source_provider}</div>
        <div class="label">Event ID</div><div class="hint">${e.event_id}</div>
      </div>`
      )
      .join("")}`;
}

async function openDetail(symbol) {
  modal.classList.remove("hidden");
  const body = document.getElementById("detail-body");
  body.innerHTML = "Loading research…";
  try {
    const r = await api(`/api/stocks/${symbol}/research`);
    const m = r.metrics;
    body.innerHTML = `
      <h2>${m.symbol}</h2>
      <div class="card-price">${fmt.price(m.price)} <span class="${pctClass(m.pct_change)}">${fmt.pct(m.pct_change)}</span></div>

      ${evidenceBarHtml(r.evidence)}

      <div class="view-badge ${viewBadgeClass(r.research_view)}">${r.research_view}</div>
      <div class="hint">${r.research_view_disclaimer}</div>

      <div class="add-symbol-row" style="margin: 8px 0;">
        <button id="save-snapshot-btn">Save Research Snapshot</button>
        <span id="save-snapshot-msg" class="hint"></span>
      </div>

      <div class="mode-toggle">
        <button class="mode-btn active" data-mode="beginner">Beginner</button>
        <button class="mode-btn" data-mode="advanced">Advanced</button>
        <span class="data-quality-badge" title="${r.data_quality.explanation}">Data quality: ${r.data_quality.level}</span>
      </div>

      <div id="beginner-panel">
        <div class="narrative-section"><h4>What is happening?</h4><p>${r.narrative.whats_happening}</p></div>
        <div class="narrative-section"><h4>Why?</h4><p>${r.narrative.why}</p></div>
        <div class="narrative-section"><h4>What looks good</h4>
          <ul class="narrative-list">${r.narrative.whats_good.map((x) => `<li>&#10003; ${x}</li>`).join("") || "<li>(none)</li>"}</ul>
        </div>
        <div class="narrative-section"><h4>Be careful</h4>
          <ul class="narrative-list">${r.narrative.be_careful.map((x) => `<li>&#9888; ${x}</li>`).join("") || "<li>(none)</li>"}</ul>
        </div>
        <div class="narrative-section"><h4>What should I watch?</h4><p>${r.narrative.what_to_watch}</p></div>

        ${
          r.catalysts.length
            ? `<div class="narrative-section"><h4>Current catalysts</h4>` +
              r.catalysts
                .map(
                  (c) => `
                <div class="catalyst-item">
                  <span class="sentiment-badge sentiment-${c.sentiment}">${c.sentiment}</span>
                  ${c.url ? `<a href="${c.url}" target="_blank" rel="noopener">${c.title}</a>` : c.title}
                  <div class="hint">${c.source}${c.published_at ? " &middot; " + c.published_at : ""}</div>
                  <div>${c.reason}</div>
                </div>`
                )
                .join("") +
              `</div>`
            : `<div class="narrative-section"><h4>Current catalysts</h4><p class="hint">${r.catalyst_note || "No clear current catalyst identified."}</p></div>`
        }

        ${eventRiskCardHtml(r.events)}
      </div>

      <div id="advanced-panel" class="advanced-panel">
        <div class="detail-metric-grid">
          <div class="label">RSI(14)</div><div>${fmt.num(m.rsi, 1)}</div>
          <div class="label">EMA 9/20/50</div><div>${fmt.num(m.ema_fast)} / ${fmt.num(m.ema_medium)} / ${fmt.num(m.ema_slow)}</div>
          <div class="label">Trend</div><div>${m.trend}</div>
          <div class="label">ATR</div><div>${fmt.num(m.atr)}</div>
          <div class="label">Relative Volume</div><div>${fmt.rvol(m.relative_volume)}</div>
          <div class="label">Volume Expansion</div><div>${fmt.rvol(m.volume_expansion)}</div>
          <div class="label">Support</div><div>${fmt.price(m.support)} (${fmt.num(m.dist_from_support_pct)}%)</div>
          <div class="label">Resistance</div><div>${fmt.price(m.resistance)} (${fmt.num(m.dist_from_resistance_pct)}%)</div>
          <div class="label">5d / 10d Momentum</div><div>${fmt.pct(m.momentum_5d_pct)} / ${fmt.pct(m.momentum_10d_pct)}</div>
          <div class="label">Volatility (ann.)</div><div>${fmt.num(m.volatility_pct)}%</div>
          <div class="label">Signal</div><div><span class="signal-badge">${m.signal}</span></div>
          <div class="label">Attention Score</div><div>${fmt.score(m.attention_score)} <span class="hint">(activity, not a rating)</span></div>
        </div>
        <h4 style="margin-top:14px;">Risk flags</h4>
        ${
          r.risk_flags.length
            ? r.risk_flags.map((f) => `<div class="risk-flag-item"><strong>${f.code}</strong> (${f.severity})<br>${f.description}</div>`).join("")
            : `<p class="hint">No risk flags triggered.</p>`
        }
        <div class="ai-box">${r.risk_explanation}</div>
        ${
          r.sector
            ? `<h4 style="margin-top:14px;">Sector context</h4>
               <p>${m.symbol} vs ${r.sector.sector_name} (${r.sector.sector_etf}): sector ${fmt.pct(r.sector.sector_pct_change)},
               market (${r.sector.market_proxy_symbol}) ${fmt.pct(r.sector.market_pct_change)},
               stock vs sector ${fmt.pct(r.sector.stock_vs_sector_pct)}.</p>`
            : `<p class="hint">Sector context unavailable for this symbol.</p>`
        }
        ${eventAdvancedDetailsHtml(r.events)}
      </div>
    `;

    body.querySelectorAll(".mode-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        body.querySelectorAll(".mode-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const advanced = btn.dataset.mode === "advanced";
        document.getElementById("advanced-panel").classList.toggle("visible", advanced);
        document.getElementById("beginner-panel").style.display = advanced ? "none" : "block";
      })
    );

    // Saves EXACTLY this displayed analysis (by analysis_id) — makes zero new Claude calls.
    document.getElementById("save-snapshot-btn").addEventListener("click", async () => {
      const msg = document.getElementById("save-snapshot-msg");
      msg.textContent = "Saving…";
      try {
        const saveResult = await api("/api/research/snapshots", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: r.analysis_id, symbol: r.symbol }),
        });
        msg.textContent = saveResult.created
          ? `Saved (snapshot #${saveResult.id}).`
          : `Already saved earlier (snapshot #${saveResult.id}).`;
      } catch (err) {
        msg.textContent = `Could not save: ${err.message}`;
      }
    });
  } catch (err) {
    body.innerHTML = `<p>Could not load research for ${symbol}: ${err.message}</p>`;
  }
}

// ---------------------------------------------------------------------------
// AI Agent chat
// ---------------------------------------------------------------------------
function appendChatMessage(role, text, toolCalls) {
  const log = document.getElementById("chat-log");
  const toolsHtml =
    toolCalls && toolCalls.length
      ? `<div class="chat-tool-calls">Used: ${toolCalls.map((t) => `${t.tool}(${Object.values(t.input || {}).join(", ")})`).join(", ")}</div>`
      : "";
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.innerHTML = `<span class="role">${role === "user" ? "You" : "Assistant"}</span>${text}${toolsHtml}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function sendChatMessage() {
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendChatMessage("user", message);
  appendChatMessage("assistant", "Thinking…");
  const log = document.getElementById("chat-log");
  const pending = log.lastElementChild;

  try {
    const result = await api("/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    pending.remove();
    appendChatMessage("assistant", result.answer, result.tool_calls);
  } catch (err) {
    pending.remove();
    appendChatMessage("assistant", `Error: ${err.message}`);
  }
}

document.getElementById("chat-send-btn").addEventListener("click", sendChatMessage);
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChatMessage();
});

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------
async function refreshAlerts() {
  const el = document.getElementById("alerts-list");
  try {
    const alerts = await api("/api/alerts");
    if (!alerts.length) {
      el.innerHTML = `<div class="hint">No alert-worthy signals right now.</div>`;
      return;
    }
    el.innerHTML = alerts
      .map(
        (a) => `
      <div class="risk-flag-item" data-symbol="${a.symbol}" style="cursor:pointer;">
        <strong>${a.symbol}</strong> — <span class="signal-badge">${a.signal}</span>
        <span class="${pctClass(a.pct_change)}">${fmt.pct(a.pct_change)}</span>
        &middot; Attention ${fmt.score(a.attention_score)}
      </div>`
      )
      .join("");
    el.querySelectorAll("[data-symbol]").forEach((row) => row.addEventListener("click", () => openDetail(row.dataset.symbol)));
  } catch (err) {
    el.innerHTML = `<div class="hint">Alerts unavailable: ${err.message}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Research Setups (entry/invalidation/targets/R:R + position sizing)
// ---------------------------------------------------------------------------
document.getElementById("setup-load-btn").addEventListener("click", async () => {
  const symbol = document.getElementById("setup-symbol-input").value.trim().toUpperCase();
  const portfolioValue = parseFloat(document.getElementById("setup-portfolio-input").value) || 10000;
  const result = document.getElementById("setup-result");
  if (!symbol) return;
  result.innerHTML = "Loading…";
  try {
    const setup = await api(`/api/stocks/${symbol}/research-setup`);
    const sizing = await api(`/api/stocks/${symbol}/position-sizing?portfolio_value=${portfolioValue}`);
    result.innerHTML = `
      <div class="section">
        <h2>${symbol} — Research Setup</h2>
        <div class="detail-metric-grid">
          <div class="label">Time Horizon</div><div>${setup.time_horizon}</div>
          <div class="label">Possible Entry Area</div><div>${fmt.price(setup.entry_zone.low)} – ${fmt.price(setup.entry_zone.high)}</div>
          <div class="label">Invalidation Level</div><div>${fmt.price(setup.invalidation_level)}</div>
          <div class="label">Support</div><div>${setup.support.map(fmt.price).join(", ") || "N/A"}</div>
          <div class="label">Resistance</div><div>${setup.resistance.map(fmt.price).join(", ") || "N/A"}</div>
          <div class="label">Possible Targets</div><div>${setup.possible_targets.map(fmt.price).join(", ") || "N/A"}</div>
          <div class="label">Risk/Reward</div><div>${setup.risk_reward_ratio != null ? setup.risk_reward_ratio + "x" : "N/A"}</div>
          <div class="label">Data Timestamp</div><div>${setup.data_timestamp}</div>
        </div>
        <div class="hint">Entry/invalidation/targets are calculated from support, resistance, and ATR — not invented by AI.</div>
      </div>
      <div class="section">
        <h2>Position Sizing (portfolio: ${fmt.price(portfolioValue)})</h2>
        <div class="detail-metric-grid">
          <div class="label">Risk per Share</div><div>${fmt.price(sizing.risk_per_share)}</div>
          <div class="label">Max Risk-Based Shares</div><div>${sizing.max_risk_based_shares ?? "N/A"}</div>
          <div class="label">Max Allocation-Based Shares</div><div>${sizing.max_allocation_based_shares ?? "N/A"}</div>
          <div class="label">Recommended Shares</div><div>${sizing.recommended_shares ?? "N/A"}</div>
          <div class="label">Position Value</div><div>${fmt.price(sizing.position_value)}</div>
          <div class="label">% of Portfolio</div><div>${sizing.position_pct_of_portfolio != null ? sizing.position_pct_of_portfolio + "%" : "N/A"}</div>
          <div class="label">Max Loss if Invalidated</div><div>${fmt.price(sizing.max_loss_if_invalidated)}</div>
        </div>
        <div class="ai-box">${sizing.explanation}</div>
        <div class="hint" style="margin-top:8px;">If you already hold this stock: ${sizing.position_adjustment_message}</div>
      </div>
      <div class="add-symbol-row">
        <button id="setup-save-snapshot-btn">Save Full Research Snapshot</button>
        <span id="setup-save-snapshot-msg" class="hint">Generates the full beginner analysis (evidence, catalysts, risk) for ${symbol} and saves it for later outcome tracking.</span>
      </div>
    `;
    document.getElementById("setup-save-snapshot-btn").addEventListener("click", async () => {
      const msg = document.getElementById("setup-save-snapshot-msg");
      msg.textContent = "Generating and saving…";
      try {
        const research = await api(`/api/stocks/${symbol}/research`);
        const saveResult = await api("/api/research/snapshots", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: research.analysis_id, symbol }),
        });
        msg.textContent = saveResult.created
          ? `Saved (snapshot #${saveResult.id}).`
          : `Already saved earlier (snapshot #${saveResult.id}).`;
      } catch (err) {
        msg.textContent = `Could not save: ${err.message}`;
      }
    });
  } catch (err) {
    result.innerHTML = `<div class="hint">Could not load setup for ${symbol}: ${err.message}</div>`;
  }
});

// ---------------------------------------------------------------------------
// Research History (Stage 2.5): saved snapshots + performance analytics
// ---------------------------------------------------------------------------
function outcomeCellHtml(outcomes, horizon) {
  const o = outcomes.find((x) => x.horizon_trading_days === horizon);
  if (!o) return `<span class="hint">N/A</span>`;
  if (o.status === "PENDING") return `<span class="hint">PENDING</span>`;
  if (o.status === "ERROR") return `<span class="pct-down">ERROR</span>`;
  return `<span class="${pctClass(o.return_pct)}">${fmt.pct(o.return_pct)}</span>`;
}

async function refreshHistory() {
  const el = document.getElementById("history-list");
  try {
    const snapshots = await api("/api/research/snapshots?limit=50");
    if (!snapshots.length) {
      el.innerHTML = `<div class="hint">No research snapshots saved yet. Open a stock's research view and click "Save Research Snapshot."</div>`;
      return;
    }
    el.innerHTML = snapshots
      .map(
        (s) => `
      <div class="risk-flag-item" data-snapshot-id="${s.id}" style="cursor:pointer;">
        <strong>${s.symbol}</strong> &middot;
        <span class="view-badge ${viewBadgeClass(s.research_view)}" style="margin:0;">${s.research_view}</span> &middot;
        <span class="pct-up">Bullish ${s.bullish_pct}%</span> &middot;
        Price at snapshot: ${fmt.price(s.price)}
        ${s.historical_validation ? `<span class="historical-badge">HISTORICAL VALIDATION</span>` : ""}
        <div class="hint" style="margin-top:4px;">
          Saved: ${new Date(s.created_at).toLocaleString()} &middot; Market snapshot: ${s.market_timestamp}
        </div>
        <div class="card-row" style="margin-top:6px;">
          <span>1D: ${outcomeCellHtml(s.outcomes, 1)}</span>
          <span>3D: ${outcomeCellHtml(s.outcomes, 3)}</span>
          <span>5D: ${outcomeCellHtml(s.outcomes, 5)}</span>
        </div>
      </div>`
      )
      .join("");
    el.querySelectorAll("[data-snapshot-id]").forEach((row) =>
      row.addEventListener("click", () => viewSavedSnapshot(row.dataset.snapshotId))
    );
  } catch (err) {
    el.innerHTML = `<div class="hint">Research history unavailable: ${err.message}</div>`;
  }
}

document.getElementById("history-refresh-outcomes-btn").addEventListener("click", async () => {
  const msg = document.getElementById("history-update-msg");
  msg.textContent = "Checking for new trading-day outcomes…";
  try {
    const result = await api("/api/research/outcomes/update", { method: "POST" });
    msg.textContent = `Checked ${result.snapshots_checked} snapshot(s): ${result.outcomes_updated} updated, ${result.still_pending} still pending${result.errors.length ? `, ${result.errors.length} error(s)` : ""}.`;
    refreshHistory();
  } catch (err) {
    msg.textContent = `Update failed: ${err.message}`;
  }
});

function groupStatsRowHtml(g) {
  return `
    <tr>
      <td>${g.label}</td>
      <td>${g.n}</td>
      <td>${fmt.pct(g.avg_return_pct)}</td>
      <td>${fmt.pct(g.median_return_pct)}</td>
      <td>${g.positive_return_frequency != null ? Math.round(g.positive_return_frequency * 100) + "%" : "N/A"}</td>
      <td>${fmt.pct(g.avg_mfe_pct)}</td>
      <td>${fmt.pct(g.avg_mae_pct)}</td>
      <td>${g.sample_warning ? `<span class="hint">${g.sample_warning}</span>` : ""}</td>
    </tr>`;
}

function groupStatsTableHtml(title, groups) {
  if (!groups || !groups.length) return "";
  return `
    <h4 style="margin-top:14px;">${title}</h4>
    <table class="data-table">
      <tr><th>Group</th><th>N</th><th>Avg Return</th><th>Median</th><th>% Positive</th><th>Avg MFE</th><th>Avg MAE</th><th>Note</th></tr>
      ${groups.map(groupStatsRowHtml).join("")}
    </table>`;
}

async function refreshPerformance() {
  const summaryEl = document.getElementById("performance-summary");
  const bucketsEl = document.getElementById("performance-buckets");
  try {
    const perf = await api("/api/research/performance");
    summaryEl.innerHTML = `
      Total saved: <strong>${perf.total_snapshots}</strong> &middot;
      Completed: <strong>${perf.completed_snapshots}</strong> &middot;
      Pending: <strong>${perf.pending_snapshots}</strong>
      ${perf.data_issues ? ` &middot; <span class="hint">${perf.data_issues} row(s) had unreadable stored data, excluded</span>` : ""}
    `;
    let html = "";
    for (const horizon of [1, 3, 5]) {
      const overall = perf.overall_by_horizon[horizon];
      if (!overall) continue;
      html += `<div class="section">`;
      html += groupStatsTableHtml(`${horizon}-Day Overall`, [overall]);
      html += groupStatsTableHtml(`${horizon}-Day by Bullish-Evidence Bucket`, perf.evidence_buckets_by_horizon[horizon]);
      html += groupStatsTableHtml(`${horizon}-Day by Research View`, perf.research_view_by_horizon[horizon]);
      html += groupStatsTableHtml(`${horizon}-Day by Catalyst Presence`, perf.catalyst_presence_by_horizon[horizon]);
      html += groupStatsTableHtml(`${horizon}-Day by Risk Flag`, perf.risk_flag_presence_by_horizon[horizon]);
      html += `</div>`;
    }
    bucketsEl.innerHTML = html || `<div class="hint">No completed outcomes yet — click "Update Outcomes Now" once enough trading days have passed.</div>`;
  } catch (err) {
    summaryEl.innerHTML = `<div class="hint">Performance analytics unavailable: ${err.message}</div>`;
  }
}

async function viewSavedSnapshot(snapshotId) {
  modal.classList.remove("hidden");
  const body = document.getElementById("detail-body");
  body.innerHTML = "Loading saved snapshot…";
  try {
    const s = await api(`/api/research/snapshots/${snapshotId}`);
    const narrative = s.narrative || {};
    body.innerHTML = `
      <h2>${s.symbol} ${s.historical_validation ? `<span class="historical-badge">HISTORICAL VALIDATION</span>` : ""}</h2>
      <div class="hint">
        Saved: ${new Date(s.created_at).toLocaleString()}<br>
        Market snapshot: ${s.market_timestamp}<br>
        Price captured: ${new Date(s.price_timestamp).toLocaleString()} (${s.price_source})
      </div>

      <h3 style="margin-top:16px;">AT THE TIME</h3>
      <div class="detail-metric-grid">
        <div class="label">Price</div><div>${fmt.price(s.price)} (${s.price_source})</div>
        <div class="label">Bullish / Neutral / Bearish</div><div>${s.bullish_pct}% / ${s.neutral_pct}% / ${s.bearish_pct}%</div>
        <div class="label">Research View</div><div>${s.research_view}</div>
        <div class="label">Signal</div><div>${s.scanner_signal}</div>
        <div class="label">Data Quality</div><div>${s.data_quality_level}</div>
      </div>
      ${
        s.catalysts.length
          ? `<div class="narrative-section"><h4>Catalysts known at that time</h4>` +
            s.catalysts.map((c) => `<div class="catalyst-item"><span class="sentiment-badge sentiment-${c.sentiment}">${c.sentiment}</span> ${c.title}</div>`).join("") +
            `</div>`
          : `<div class="hint">No catalyst recorded at that time.</div>`
      }
      ${
        s.risk_flags.length
          ? `<div class="narrative-section"><h4>Risks known at that time</h4>` +
            s.risk_flags.map((f) => `<div class="risk-flag-item"><strong>${f.code}</strong> (${f.severity})<br>${f.description}</div>`).join("") +
            `</div>`
          : ""
      }
      <div class="narrative-section">
        <h4>Events known at the time ${s.events ? `<span class="event-risk-badge event-risk-${eventRiskBadgeClass(s.events.event_risk_level)}">${s.events.event_risk_level}</span>` : ""}</h4>
        ${
          s.events
            ? [...s.events.company_events, ...s.events.macro_events].length
              ? [...s.events.company_events, ...s.events.macro_events].map(eventLineHtml).join("")
              : `<p class="hint">No event was recorded within the tracked window at analysis time.</p>`
            : `<p class="hint">Event source unavailable at the time of analysis.</p>`
        }
      </div>
      <div class="narrative-section"><h4>Agent explanation at that time</h4>
        <p>${narrative.whats_happening || ""}</p>
        <p>${narrative.why || ""}</p>
      </div>

      <h3 style="margin-top:16px;">WHAT HAPPENED AFTERWARD</h3>
      <div class="hint">Measured independently from the "at the time" section above — never mixed together.</div>
      ${s.outcomes
        .map(
          (o) => `
        <div class="detail-metric-grid" style="margin-top:8px;">
          <div class="label">${o.horizon_trading_days} trading day(s)</div>
          <div>${
            o.status === "PENDING"
              ? "PENDING — not enough trading sessions have elapsed yet."
              : o.status === "ERROR"
              ? `ERROR: ${o.error_message}`
              : `${fmt.pct(o.return_pct)} (price ${fmt.price(o.price_at_horizon)}) &middot; MFE ${fmt.pct(o.max_favorable_excursion_pct)} &middot; MAE ${fmt.pct(o.max_adverse_excursion_pct)}` +
                (o.did_hit_target_1 != null ? ` &middot; Target 1: ${o.did_hit_target_1 ? "HIT" : "not hit"}` : "") +
                (o.did_hit_invalidation != null ? ` &middot; Invalidation: ${o.did_hit_invalidation ? "HIT" : "not hit"}` : "") +
                (o.sequencing_ambiguous ? ` &middot; <span class="hint">order within one trading day is ambiguous from daily bars</span>` : "")
          }</div>
        </div>`
        )
        .join("")}
      <div class="hint" style="margin-top:10px;">Historical observation only — not a guarantee of future performance.</div>
    `;
  } catch (err) {
    body.innerHTML = `<p>Could not load snapshot #${snapshotId}: ${err.message}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Boot + polling
// ---------------------------------------------------------------------------
function refreshAll() {
  refreshStatus();
  refreshAIUsage();
  refreshOverview();
  refreshWatchlist();
  refreshAlerts();
}

refreshAll();
setInterval(refreshAll, REFRESH_MS);
