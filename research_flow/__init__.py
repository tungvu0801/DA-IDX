"""
research_flow/ — the research/decision workflow orchestration layer.

    market data → deterministic scanner / ranker (Stage 4.7 rotation run) → small SHORTLIST → Claude research ONLY on the
    shortlist (structured, bounded, guarded) → cached results → observability → the deterministic proposal (unchanged)

Python owns scanning, ranking, shortlist selection, portfolio math, risk and eligibility rules, budgets and broker safety.
Claude owns only qualitative synthesis (catalysts, risks, earnings / news context, a concise summary) and is never
required for the deterministic results, never decides a trade, a size or a price, and never reaches a broker. If Claude
is unavailable, every deterministic result is unchanged and the research section reports it.

  contracts.py     ResearchRequest (deterministic context only) · ResearchResult parsing that fails closed
  shortlist.py     deterministic shortlist-before-LLM selection (top N, held deterioration, rank movers, user picks)
  store.py         local append-only cache + batch observability (database/research_cache_migrations.py)
  orchestrator.py  the state machine, budgets, cache, the single gated Claude call per symbol, failure handling
"""
