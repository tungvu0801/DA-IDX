"""
agents/catalyst_agent.py — Real Catalyst Agent.

Pulls real, current articles from data/news.py (Alpaca's news feed) and
asks Claude to classify each one's relevance and sentiment. Claude only
ever classifies text it's actually given here — it is explicitly forbidden
from adding a catalyst that wasn't in the fetched articles, or from using
outside knowledge of the company. If no articles are found, or none
classify as relevant, the result says so explicitly rather than forcing an
explanation.

Uses the same shared gating pipeline (agents/gating.py) as the Technical
Agent: Attention Score threshold (bypassable for user-requested analysis),
AI cache, then hourly/daily call limits.
"""
import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import config
from agents.gating import UNAVAILABLE_MESSAGE, run_gated_agent, strip_markdown_fence
from agents.technical_agent import get_provider
from data.news import get_recent_news

logger = logging.getLogger(__name__)

NO_CATALYST_MESSAGE = "No clear current catalyst identified."

_SENTIMENTS = ("POSITIVE", "NEUTRAL", "NEGATIVE", "UNCERTAIN")

_SYSTEM_PROMPT = """You are a news-classification assistant for a personal stock research dashboard.

You will be told a target stock ticker, then given a list of real news articles (title,
source, timestamp, summary) fetched from Alpaca's news feed using that ticker as a search
filter. Alpaca tags an article with a symbol whenever it's MENTIONED, even if the article is
really about a competitor or the sector generally — so do not assume every article is
actually about the target company. Classify EACH article using ONLY the text given — do not
add an article that isn't in the list, and do not use your own outside knowledge of the
company, its business, or recent events beyond what's in the article text.

For each article return:
  - "relevant": true ONLY if the article is substantively about the target ticker itself
(its business, results, guidance, products, or a direct catalyst for it) and plausibly
explains why THIS stock specifically might be moving. false if the target ticker is only
mentioned in passing, as a comparison, or as context for a story that is really about a
different company or the broader sector.
  - "sentiment": one of POSITIVE, NEUTRAL, NEGATIVE, UNCERTAIN for the target ticker
specifically — UNCERTAIN if the article's implication for it is genuinely ambiguous, not
just because the text is short. Irrelevant articles should still get a best-effort sentiment.
  - "reason": one short sentence explaining the classification, naming the target ticker.

Respond with ONLY a JSON array, one object per input article, in the same order, with keys
"relevant", "sentiment", "reason". No prose before or after the JSON."""


@dataclass
class CatalystItem:
    title: str
    source: str
    published_at: Optional[str]
    url: str
    summary: str
    relevant: bool
    sentiment: str  # POSITIVE | NEUTRAL | NEGATIVE | UNCERTAIN
    reason: str


@dataclass
class CatalystAnalysis:
    symbol: str
    items: List[CatalystItem]
    message: str  # NO_CATALYST_MESSAGE, an unavailable/error message, or "" when items exist


def _build_prompt(symbol: str, articles: List[dict]) -> str:
    payload = [
        {
            "title": a.get("headline", ""),
            "source": a.get("source", ""),
            "timestamp": str(a.get("created_at", "")),
            "summary": a.get("summary", ""),
        }
        for a in articles
    ]
    return f"Target ticker: {symbol}\n\nClassify these articles:\n\n" + json.dumps(payload, indent=2, default=str)


def _parse_classification(raw_text: str, articles: List[dict]) -> Optional[List[CatalystItem]]:
    """Parse Claude's JSON array response into CatalystItems. Returns None on any malformed output."""
    try:
        parsed = json.loads(strip_markdown_fence(raw_text))
    except (json.JSONDecodeError, TypeError):
        logger.warning("Catalyst agent returned non-JSON output; treating as unavailable.")
        return None

    if not isinstance(parsed, list) or len(parsed) != len(articles):
        logger.warning("Catalyst agent returned malformed/mismatched classification list.")
        return None

    items: List[CatalystItem] = []
    for article, classification in zip(articles, parsed):
        if not isinstance(classification, dict):
            return None
        sentiment = classification.get("sentiment")
        if sentiment not in _SENTIMENTS:
            sentiment = "UNCERTAIN"
        items.append(
            CatalystItem(
                title=article.get("headline", ""),
                source=article.get("source", ""),
                published_at=str(article.get("created_at")) if article.get("created_at") else None,
                url=article.get("url", ""),
                summary=article.get("summary", ""),
                relevant=bool(classification.get("relevant", False)),
                sentiment=sentiment,
                reason=str(classification.get("reason", "")),
            )
        )
    return items


def get_catalyst_analysis(
    symbol: str, attention_score: int, price: float, signal: str, user_requested: bool = False
) -> CatalystAnalysis:
    """
    Fetch real news for `symbol` and classify it. Gated the same way as the
    Technical Agent. Returns a CatalystAnalysis whose `.items` is empty and
    `.message` explains why whenever nothing could be produced (no news
    found, no LLM configured, rate-limited, etc.) — never a fabricated catalyst.
    """
    articles = get_recent_news(symbol)
    if not articles:
        return CatalystAnalysis(symbol=symbol, items=[], message=NO_CATALYST_MESSAGE)

    raw_result = run_gated_agent(
        symbol=symbol,
        price=price,
        attention_score=attention_score,
        signal=signal,
        analysis_type="catalyst",
        system_prompt=_SYSTEM_PROMPT,
        get_provider_fn=get_provider,
        build_prompt_fn=lambda: _build_prompt(symbol, articles),
        user_requested=user_requested,
        max_tokens=2500,  # classifying up to ~10 articles needs more headroom than a single explanation
    )

    if raw_result == UNAVAILABLE_MESSAGE or raw_result.startswith("AI analysis"):
        # Skipped by the threshold, cache-miss-but-rate-limited, unavailable, or a provider
        # error — `raw_result` is already a clear human-readable explanation in every case.
        return CatalystAnalysis(symbol=symbol, items=[], message=raw_result)

    # Strip the cache-hit suffix (if present) before attempting to parse JSON.
    json_text = raw_result.split("\n\n(Cached analysis")[0]
    items = _parse_classification(json_text, articles)
    if items is None:
        return CatalystAnalysis(
            symbol=symbol,
            items=[],
            message="Catalyst classification returned an unexpected format; treating as unavailable.",
        )

    relevant_items = [item for item in items if item.relevant]
    if not relevant_items:
        return CatalystAnalysis(symbol=symbol, items=[], message=NO_CATALYST_MESSAGE)

    return CatalystAnalysis(symbol=symbol, items=relevant_items, message="")
