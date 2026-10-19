"""Prompt templates and JSON schemas for every LLM purpose."""

from __future__ import annotations

from typing import Any

from vantage.core.util import dumps

# ----------------------------------------------------------------------------- trade gate
GATE_SYSTEM = """You are the final risk reviewer on a quantitative trading desk.
A deterministic pipeline has ALREADY found a statistical edge and sized the trade with fractional Kelly.
Your job is to catch what the numbers cannot see: stale or contradictory news, imminent binary
events (earnings, FDA, court rulings, macro releases), signals that disagree with each other,
illiquidity, or a thesis that is obviously already priced in.

Rules:
- You can only APPROVE, REDUCE (size_multiplier between 0.1 and 0.9) or REJECT. You can never increase size.
- Default to APPROVE when the evidence is consistent and no red flag exists — do not reject merely
  because the future is uncertain; the edge is statistical.
- REJECT if there is a concrete, specific reason the edge is likely invalid right now.
- Be terse and specific. Cite the evidence fields you used.

How to read the dossier (we are always the BUYER of the option or the stock):
- signals[].score is in [-1, 1]; positive = bullish. forecast.z is the combined strength; forecast.prob_up is
  the modelled probability the underlying rises over the horizon.
- scan.ev_pct = expected return on the premium at the planned exit, already net of spread and theta.
- scan.iv_rv_ratio = implied vol / our forecast vol. ABOVE 1 means options are EXPENSIVE (bad for a buyer,
  the move must be larger to profit); BELOW 1 means options are cheap (good for a buyer).
- scan.iv_rank near 1 = IV high vs its own history (expensive); near 0 = cheap.
- events.days_to_earnings: null = no known date; small numbers mean an imminent binary event and IV crush risk.
- news[].sentiment in [-1, 1]; options_flow.score > 0 = bullish aggressive flow.
- A bearish trade (long put) wants negative scores / bearish flow; a bullish trade wants the opposite.
Return only JSON matching the schema."""

GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reduce", "reject"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "size_multiplier": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "key_risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "size_multiplier", "rationale"],
}


def gate_user(dossier: dict[str, Any]) -> str:
    return "Trade dossier (JSON):\n" + dumps(dossier).decode() + "\n\nDecide."


# ----------------------------------------------------------------------------- news -> signal
NEWS_SYSTEM = """You convert financial news into trading signals for the named tickers.
For each ticker that the article is materially about, estimate:
- direction: -1 (clearly bearish) .. +1 (clearly bullish) for the stock price
- magnitude: 0..1 expected size of impact relative to the stock's normal daily move (1 = multiple days' move)
- relevance: 0..1 how central the ticker is to the article
- novelty: 0..1 whether this is new information (0 = recap/opinion/old news, 1 = fresh material fact)
- horizon: "intraday", "days" or "weeks"
Routine filings, conference appearances and recaps are near zero. Analyst actions are moderate.
Earnings beats/misses with guidance changes, M&A, regulatory actions are large.
Return only JSON matching the schema."""

NEWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "direction": {"type": "number", "minimum": -1, "maximum": 1},
                    "magnitude": {"type": "number", "minimum": 0, "maximum": 1},
                    "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                    "novelty": {"type": "number", "minimum": 0, "maximum": 1},
                    "horizon": {"type": "string", "enum": ["intraday", "days", "weeks"]},
                    "rationale": {"type": "string"},
                },
                "required": ["symbol", "direction", "magnitude", "relevance", "novelty", "horizon"],
            },
        }
    },
    "required": ["items"],
}


def news_user(headline: str, summary: str, symbols: list[str]) -> str:
    return f"Tickers: {', '.join(symbols)}\nHeadline: {headline}\nSummary: {summary[:1500]}"


# ----------------------------------------------------------------------------- analyst strategy
ANALYST_SYSTEM = """You are a discretionary portfolio manager with a quantitative toolkit.
You receive a dossier for one underlying: ensemble signal forecast, individual signal scores,
volatility (implied vs forecast), recent news with sentiment, unusual options flow, and upcoming events.
Decide whether to express a directional view with a long call (bullish) or long put (bearish), or pass.
Only take trades where several independent evidence types agree. Pass when evidence conflicts.
Reading the numbers: signal scores are in [-1, 1] (positive = bullish); volatility.atm_iv above
volatility.forecast_vol means options are EXPENSIVE for a buyer (needs a bigger move) and below means cheap;
events.days_to_earnings small = imminent binary event and IV crush; options_flow.score > 0 = bullish flow.
Return only JSON matching the schema."""

ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["long_call", "long_put", "pass"]},
        "conviction": {"type": "number", "minimum": 0, "maximum": 1},
        "horizon_days": {"type": "number", "minimum": 1, "maximum": 60},
        "rationale": {"type": "string"},
    },
    "required": ["action", "conviction", "horizon_days", "rationale"],
}
