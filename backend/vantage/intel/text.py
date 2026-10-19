"""Text analytics for intel items: ticker extraction, retail-slang sentiment, statement impact, theme tagging.

These are deliberately simple, transparent lexicons — the LLM (when running) re-scores statements; the lexicon keeps
everything working offline. Retail sarcasm and context are out of reach for a lexicon, so every score carries a
``strength`` and the signal layer treats social data as a low-weight, IC-tracked input.
"""

from __future__ import annotations

import html
import math
import re
from typing import Any

from vantage.data.news import lexicon_sentiment

CASHTAG = re.compile(r"(?<![A-Za-z0-9$])\$([A-Za-z]{1,5})(?![A-Za-z0-9])")
TOKEN = re.compile(r"[a-z][a-z'\-]+|[\U0001F300-\U0001FAFF]")
BLOCK_TAGS = re.compile(r"</?(?:p|br|div|li|ul|ol|h\d|blockquote)\b[^>]*>", re.I)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")

# Words that are tickers but overwhelmingly ordinary English: only count as a mention with a cashtag.
AMBIGUOUS = {
    "A", "ALL", "ARE", "BIG", "CAN", "FOR", "FAST", "IT", "NOW", "ONE", "OPEN", "REAL", "SEE", "TWO", "WELL", "NEW",
    "OUT", "LOW", "HAS", "GOOD", "LOVE", "PLAY", "SAVE", "TRUE", "ON", "ALLY", "BEST", "HOPE", "LIFE", "MAIN", "PEAK",
}

NAME_TO_SYMBOL = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN", "meta": "META", "facebook": "META",
    "google": "GOOGL", "alphabet": "GOOGL", "tesla": "TSLA", "netflix": "NFLX", "broadcom": "AVGO", "amd": "AMD",
    "intel": "INTC", "palantir": "PLTR", "coinbase": "COIN", "uber": "UBER", "jpmorgan": "JPM", "boeing": "BA",
    "micron": "MU", "qualcomm": "QCOM", "oracle": "ORCL", "salesforce": "CRM", "disney": "DIS", "walmart": "WMT",
    "exxon": "XOM", "chevron": "CVX", "pfizer": "PFE", "eli lilly": "LLY", "novo nordisk": "NVO", "tsmc": "TSM",
    "taiwan semiconductor": "TSM", "microstrategy": "MSTR", "strategy inc": "MSTR", "robinhood": "HOOD",
    "gamestop": "GME", "amc": "AMC", "ford": "F", "general motors": "GM", "lockheed": "LMT", "raytheon": "RTX",
    "bitcoin": "COIN", "s&p 500": "SPY", "s&p500": "SPY", "nasdaq": "QQQ", "dow jones": "DIA", "russell 2000": "IWM",
}

# theme -> (keywords, symbols most exposed). Symbols are only used when they are in the active universe.
THEMES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "tariffs": (("tariff", "trade war", "trade deal", "import dut", "export control", "customs"),
                ("AAPL", "TSLA", "NVDA", "AMD", "AVGO", "SMH", "QQQ", "IWM", "F", "GM", "BA")),
    "fed": (("federal reserve", "the fed", "powell", "fomc", "rate cut", "rate hike", "interest rate", "rates", "dovish",
             "hawkish", "inflation", "cpi", "treasury yield"), ("SPY", "QQQ", "IWM", "XLF", "JPM")),
    "china": (("china", "chinese", "beijing", "xi jinping", "taiwan", "yuan", "hong kong"),
              ("BABA", "FXI", "KWEB", "NVDA", "AAPL", "TSLA", "SMH", "TSM")),
    "geopolitics": (("war", "ceasefire", "invade", "invasion", "missile", "sanction", "nato", "russia", "ukraine", "iran",
                     "israel", "gaza", "north korea", "houthi", "strait"), ("XLE", "LMT", "RTX", "SPY", "GLD")),
    "energy": (("oil", "opec", "crude", "natural gas", "drilling", "pipeline", "lng", "gasoline"),
               ("XLE", "XOM", "CVX", "OXY")),
    "crypto": (("bitcoin", "crypto", "ethereum", "stablecoin", "sec crypto", "digital asset", "btc"),
               ("COIN", "MSTR", "HOOD", "MARA", "RIOT")),
    "tech": (("artificial intelligence", " ai ", "semiconductor", "chip", "data center", "antitrust", "big tech",
              "nvidia", "openai", "section 230"), ("NVDA", "AMD", "AVGO", "SMH", "MSFT", "GOOGL", "META", "QQQ", "XLK")),
    "banks": (("bank", "banking", "capital requirement", "basel", "stress test", "credit card", "fdic"),
              ("JPM", "XLF", "BAC", "GS", "C", "WFC")),
    "health": (("drug pricing", "medicare", "fda", "pharma", "vaccine", "obesity", "healthcare"),
               ("LLY", "PFE", "MRK", "XLV", "UNH", "NVO")),
    "fiscal": (("shutdown", "debt ceiling", "stimulus", "tax cut", "tax bill", "budget", "spending bill", "default"),
               ("SPY", "IWM", "TLT")),
    "autos": (("electric vehicle", " ev ", "auto tariff", "autos", "car makers"), ("TSLA", "F", "GM")),
    "defense": (("defense", "pentagon", "military", "nato spending"), ("LMT", "RTX", "NOC", "GD")),
    "elections": (("election", "midterm", "poll", "campaign", "ballot"), ("SPY",)),
}

# Statement lexicon: phrase -> equity-market impact weight (positive = bullish). Matched as substrings of lowercase text.
STMT_BULL = {
    "trade deal": 1.0, "deal reached": 1.0, "reached a deal": 1.0, "agreement": 0.5, "pause tariff": 1.2,
    "pausing tariff": 1.2, "tariff pause": 1.2, "reduce tariff": 1.0, "lower tariff": 1.0, "tariff relief": 1.2,
    "exempt": 0.6, "rate cut": 0.9, "cut rates": 0.9, "lower rates": 0.8, "dovish": 0.8, "ceasefire": 0.8, "peace": 0.5,
    "stimulus": 0.7, "tax cut": 0.7, "deregulat": 0.6, "lift sanctions": 0.6, "lifting sanctions": 0.6,
    "record high": 0.5, "great economy": 0.3, "invest": 0.4, "billion investment": 0.7, "jobs": 0.2, "approved": 0.4,
    "strong economy": 0.4, "waive": 0.7, "waives": 0.7, "waiver": 0.5, "scrap": 0.6, "rolls back": 0.8, "roll back": 0.8,
    "eases": 0.5, "easing": 0.4, "growth": 0.3, "soft landing": 0.7, "agreed to": 0.4, "extend": 0.2, "delay tariff": 0.9,
}
STMT_BEAR = {
    "tariff": 0.8, "trade war": 1.2, "sanction": 0.6, "export control": 0.8, "ban": 0.5, "shutdown": 0.8,
    "escalat": 0.9, "invade": 1.2, "invasion": 1.2, "strike on": 0.6, "missile": 0.8, "rate hike": 0.9,
    "raise rates": 0.8, "hawkish": 0.8, "recession": 1.0, "default": 1.0, "retaliat": 0.9, "threaten": 0.7,
    "crack down": 0.5, "break up": 0.7, "antitrust": 0.6, "investigat": 0.5, "subpoena": 0.5, "impose": 0.6,
    "100%": 0.5, "dumping": 0.3, "fire": 0.3, "fired": 0.4, "inflation": 0.4, "debt crisis": 1.0, "war": 0.7,
    "nuclear": 0.6, "terror": 0.8, "emergency": 0.6, "collapse": 1.0, "crisis": 0.8,
}
NEGATORS = ("no ", "not ", "never ", "without ", "end ", "ending ", "stop ", "stops ", "ends ", "removes ", "remove ")

SOCIAL_BULL = {
    "bullish": 1.0, "calls": 0.5, "call": 0.2, "moon": 0.8, "mooning": 0.9, "rocket": 0.7, "squeeze": 0.6,
    "breakout": 0.7, "breaking out": 0.7, "buy the dip": 0.8, "btfd": 0.8, "ath": 0.5, "all time high": 0.6,
    "tendies": 0.6, "undervalued": 0.6, "long": 0.3, "going up": 0.5, "rip": 0.4, "ripping": 0.6, "green": 0.3,
    "load up": 0.6, "loading": 0.3, "accumulate": 0.5, "beat": 0.4, "bounce": 0.4, "rally": 0.6, "to the moon": 0.9,
    "🚀": 0.9, "🐂": 0.8, "📈": 0.6, "💎": 0.4, "🟢": 0.3, "bull": 0.6, "upside": 0.4, "strong": 0.2,
}
SOCIAL_BEAR = {
    "bearish": 1.0, "puts": 0.5, "put": 0.2, "dump": 0.8, "dumping": 0.9, "crash": 0.9, "crashing": 1.0, "rug": 0.8,
    "rugpull": 1.0, "short": 0.3, "shorting": 0.5, "overvalued": 0.6, "bubble": 0.6, "bagholder": 0.5, "bagholding": 0.6,
    "drill": 0.6, "tank": 0.5, "tanking": 0.8, "sell": 0.4, "selling": 0.3, "fade": 0.4, "fraud": 0.8, "scam": 0.8,
    "bankrupt": 1.0, "red": 0.3, "🐻": 0.8, "📉": 0.6, "🔴": 0.3, "💀": 0.4, "downside": 0.4, "bear": 0.6, "weak": 0.2,
    "dead cat": 0.7, "falling knife": 0.6, "overbought": 0.4, "bleeding": 0.6,
}


def clean_text(raw: str) -> str:
    """HTML -> plain text, collapsed whitespace."""
    return WS.sub(" ", html.unescape(TAGS.sub("", BLOCK_TAGS.sub(" ", raw or "")))).strip()


def extract_symbols(text: str, universe: set[str] | frozenset[str], *, cashtags_any: bool = False) -> list[str]:
    """Tickers mentioned in ``text``.

    Cashtags count when the symbol is in the universe (or always with ``cashtags_any``). Bare UPPERCASE tokens count only
    for universe symbols with >= 2 letters that are not common English words; company names map through NAME_TO_SYMBOL.
    """
    found: dict[str, None] = {}
    for m in CASHTAG.finditer(text):
        s = m.group(1).upper()
        if s in universe or cashtags_any:
            found[s] = None
    for m in re.finditer(r"\b[A-Z]{2,5}\b", text):
        s = m.group(0)
        if s in universe and s not in AMBIGUOUS:
            found[s] = None
    low = text.lower()
    for name, sym in NAME_TO_SYMBOL.items():
        if sym in universe and re.search(rf"\b{re.escape(name)}\b", low):
            found[sym] = None
    return list(found)


def _score(low: str, bull: dict[str, float], bear: dict[str, float], negate: bool) -> tuple[float, float]:
    """Sum matched phrase weights. Longest phrases match first and are masked, so 'pause tariff' (bullish) is not
    also counted as the bearish 'tariff'; a negator just before a phrase ('no tariffs') flips it."""
    phrases = sorted([(k, w, True) for k, w in bull.items()] + [(k, w, False) for k, w in bear.items()],
                     key=lambda x: -len(x[0]))
    pos = neg = 0.0
    for k, w, is_bull in phrases:
        i = low.find(k)  # each phrase counts once per text
        if i < 0:
            continue
        flipped = negate and any(low[max(0, i - 12):i].endswith(n) for n in NEGATORS)
        if is_bull != flipped:
            pos += w
        else:
            neg += w
        low = low[:i] + " " * len(k) + low[i + len(k):]
    return pos, neg


def social_sentiment(text: str) -> tuple[float, float]:
    """(sentiment in [-1, 1], strength in [0, 1]) from retail slang + the finance lexicon (at half weight)."""
    low = " " + text.lower() + " "
    pos, neg = _score(low, SOCIAL_BULL, SOCIAL_BEAR, negate=True)
    fs, fstrength = lexicon_sentiment(text)
    pos += 0.5 * max(fs, 0) * fstrength * 2
    neg += 0.5 * max(-fs, 0) * fstrength * 2
    if pos == 0 and neg == 0:
        return 0.0, 0.0
    s = (pos - neg) / (pos + neg + 1.0)
    return clip1(s), 1.0 - math.exp(-(pos + neg) / 2.0)


def statement_impact(text: str) -> tuple[float, float]:
    """(market tilt in [-1, 1], strength in [0, 1]) for a statement by a market mover; bearish words weigh a bit more."""
    low = " " + text.lower() + " "
    pos, neg = _score(low, STMT_BULL, STMT_BEAR, negate=True)
    if pos == 0 and neg == 0:
        return 0.0, 0.0
    s = (pos - 1.1 * neg) / (pos + neg + 1.5)
    return clip1(s), 1.0 - math.exp(-(pos + neg) / 2.0)


def clip1(x: float) -> float:
    return max(-1.0, min(1.0, x))


def themes_of(text: str) -> list[str]:
    low = " " + text.lower() + " "
    return [t for t, (kw, _) in THEMES.items() if any(k in low for k in kw)]


def theme_symbols(themes: list[str], universe: set[str] | frozenset[str]) -> list[str]:
    out: dict[str, None] = {}
    for t in themes:
        for s in THEMES[t][1]:
            if s in universe:
                out[s] = None
    return list(out)


# ---------------------------------------------------------------------------- prediction-market classification
# theme -> (keywords, polarity of a higher 'yes' probability for US equities, weight)
PM_THEMES: list[tuple[str, tuple[str, ...], int, float]] = [
    ("recession", ("recession", "gdp contract", "negative gdp", "unemployment rate above", "jobless"), -1, 1.0),
    ("fed", ("fed ", "fomc", "interest rate", "rate cut", "rate hike", "federal funds", "powell", "fed chair"), 0, 0.8),
    ("inflation", ("inflation", "cpi", "pce"), -1, 0.6),
    ("tariffs", ("tariff", "trade deal", "trade war", "trade agreement"), -1, 0.9),
    ("shutdown", ("shutdown", "debt ceiling", "government funding"), -1, 0.7),
    ("geopolitics", ("war ", "invade", "invasion", "ceasefire", "strike on", "nuclear", "taiwan", "ukraine", "russia",
                     "iran", "israel", "china"), -1, 0.8),
    ("markets", ("s&p 500", "s&p500", "nasdaq", "dow jones", "spx", "stock market", "bull market", "bear market",
                 "vix"), 0, 0.7),
    ("crypto", ("bitcoin", "ethereum", "crypto", "btc"), 0, 0.3),
    ("energy", ("crude", "oil price", "wti", "brent", "gas price", "opec"), 0, 0.4),
    ("election", ("election", "president", "senate", "house majority", "midterm", "democrat", "republican", "nominee"),
     0, 0.5),
]
# Questions where a *higher* probability is bullish / bearish even though the theme polarity is 0 or opposite.
PM_BULL_HINT = ("rate cut", "cut rates", "decrease interest", "lower rates", "ceasefire", "trade deal", "agreement",
                "above", "new all-time high", "reach", "bull market", "soft landing", "rise", "higher")
PM_BEAR_HINT = ("rate hike", "hike", "recession", "shutdown", "default", "invade", "invasion", "crash", "below", "drop",
                "fall", "bear market", "downgrade", "layoffs", "tariff", "war")
PM_IGNORE = ("bank of canada", "bank of england", "bank of japan", "bank of mexico", "banxico", "ecb ", "boj ", "riksbank",
             "norges", "reserve bank of", "snb ", "pboc", "turkey", "argentina", "brazil", "india ", "canadian", "super bowl", "world cup", "nba", "nfl", "nhl", "mlb", "ufc", "premier league", "champions league",
             "grammy", "oscar", "emmy", "taylor swift", "album", "box office", "tweet", "mrbeast", "celebrity", "pope",
             "eurovision", "esports", "tennis", "golf", "f1 ", "formula 1", "kxmve", "win the 20", "wins the 20")


def classify_market(question: str) -> tuple[list[str], int, float] | None:
    """(themes, equity polarity of a higher 'yes' probability, weight) or None when it is not market-relevant."""
    low = " " + question.lower() + " "
    if any(k in low for k in PM_IGNORE):
        return None
    themes: list[str] = []
    pol = 0
    weight = 0.0
    for name, kws, p, w in PM_THEMES:
        if any(k in low for k in kws):
            themes.append(name)
            if not pol:
                pol = p
            weight = max(weight, w)
    if not themes:
        return None
    # explicit direction in the wording beats the theme default (e.g. a fed question is dovish for "cut", hawkish for "hike")
    bull = sum(1 for k in PM_BULL_HINT if k in low)
    bear = sum(1 for k in PM_BEAR_HINT if k in low)
    if "fed" in themes or "markets" in themes or "crypto" in themes or "energy" in themes or "election" in themes:
        pol = 1 if bull > bear else -1 if bear > bull else 0
    elif bull > bear + 1:
        pol = 1
    return themes, pol, weight


def short(text: str, n: int = 280) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def to_wire_stats(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}
