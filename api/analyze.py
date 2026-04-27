import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

logger = logging.getLogger(__name__)

import anthropic
import yfinance as yf
from finvizfinance.quote import finvizfinance as fvf
from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from timing_rules import compute_timing

app = Flask(__name__)
CORS(app)

CLAUDE_MODEL      = "claude-sonnet-4-6"  # narrative + peer identification
CLAUDE_MODEL_FAST = "claude-haiku-4-5-20251001"   # ticker resolution only

_spy_cache = {"perf_year": None, "ts": 0}
_SPY_TTL = 86400  # 24 hours

_ticker_cache = {}  # {ticker: {"date": date, "result": dict}}

MIN_PEERS = 3  # minimum peers required for meaningful relative scoring

US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "PCX", "ASE", "NYSEArca", "BATS", "PNK", "OTC", "NASDAQ", "NYSE"}

CURRENCY_SYMBOLS = {
    "USD": "$",   "EUR": "€",   "GBP": "£",   "HKD": "HK$", "JPY": "¥",
    "CNY": "¥",   "AUD": "A$",  "CAD": "C$",  "CHF": "CHF ", "SEK": "SEK ",
    "NOK": "NOK ","DKK": "DKK ","KRW": "₩",   "INR": "₹",   "SGD": "S$",
    "BRL": "R$",  "MXN": "MX$", "ZAR": "R ",  "TWD": "NT$",
    "MYR": "RM ", "IDR": "Rp ", "THB": "฿",   "PHP": "₱",   "VND": "₫",
}


def get_spy_perf():
    if time.time() - _spy_cache["ts"] < _SPY_TTL and _spy_cache["perf_year"] is not None:
        return _spy_cache["perf_year"]
    try:
        f = fvf("SPY").ticker_fundament()
        val = parse_num(f.get("Perf Year"))
        _spy_cache["perf_year"] = val
        _spy_cache["ts"] = time.time()
        return val
    except Exception:
        return _spy_cache["perf_year"]


def parse_num(val):
    if not val or val in ("-", "N/A", ""):
        return None
    val = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
    multipliers = {"B": 1e9, "M": 1e6, "K": 1e3, "T": 1e12}
    if val[-1] in multipliers:
        try:
            return float(val[:-1]) * multipliers[val[-1]]
        except ValueError:
            return None
    try:
        return float(val)
    except ValueError:
        return None


def fetch_fundamentals_finviz(ticker):
    stock = fvf(ticker)
    f = stock.ticker_fundament()

    market_cap_raw = parse_num(f.get("Market Cap"))
    market_cap_b = round(market_cap_raw / 1e9, 2) if market_cap_raw else None

    ev_raw = parse_num(f.get("Enterprise Value"))
    ev_b = ev_raw / 1e9 if ev_raw else None

    ev_ebitda = parse_num(f.get("EV/EBITDA"))

    # Net Debt/EBITDA = (EV - MarketCap) / (EV / EV_EBITDA)
    net_debt_ebitda = None
    if ev_b and market_cap_b and ev_ebitda and ev_ebitda != 0:
        ebitda_b = ev_b / ev_ebitda
        net_debt_ebitda = round((ev_b - market_cap_b) / ebitda_b, 2) if ebitda_b else None

    pfcf = parse_num(f.get("P/FCF"))
    fcf_yield = round(100 / pfcf, 2) if pfcf else None

    # Dividend yield is buried in "Dividend TTM" as "1.04 (0.38%)" — extract the pct
    div_ttm = f.get("Dividend TTM", "")
    div_match = re.search(r"\(([\d.]+)%\)", div_ttm or "")
    dividend_yield = float(div_match.group(1)) if div_match else None

    return {
        "ticker": ticker.upper(),
        "company_name": f.get("Company", ticker.upper()),
        "sector": f.get("Sector", ""),
        "industry": f.get("Industry", ""),
        "market_cap_b": market_cap_b,
        "pe_ratio": parse_num(f.get("P/E")),
        "forward_pe": parse_num(f.get("Forward P/E")),
        "ev_ebitda": ev_ebitda,
        "ps_ratio": parse_num(f.get("P/S")),
        "pb_ratio": parse_num(f.get("P/B")),
        "gross_margin": parse_num(f.get("Gross Margin")),
        "operating_margin": parse_num(f.get("Oper. Margin")),
        "net_margin": parse_num(f.get("Profit Margin")),
        "roe": parse_num(f.get("ROE")),
        "roic": parse_num(f.get("ROIC")),
        "revenue_growth_yoy": parse_num(f.get("Sales Y/Y TTM")),
        "eps_growth_yoy": parse_num(f.get("EPS Y/Y TTM")),
        "current_ratio": parse_num(f.get("Current Ratio")),
        "debt_eq": parse_num(f.get("Debt/Eq")),
        "net_debt_ebitda": net_debt_ebitda,
        "fcf_yield": fcf_yield,
        "dividend_yield": dividend_yield,
        "analyst_recom": f.get("Recom"),
        "current_price": parse_num(f.get("Price")),
        "target_price":  parse_num(f.get("Target Price")),
        "perf_year":     parse_num(f.get("Perf Year")),
        "perf_month":    parse_num(f.get("Perf Month")),
        "short_float":   parse_num(f.get("Short Float")),
        "short_ratio":   parse_num(f.get("Short Ratio")),
        "rsi":           parse_num(f.get("RSI (14)")),
        "vs_sma50":      parse_num(f.get("SMA50")),
        "vs_sma200":     parse_num(f.get("SMA200")),
        "pct_from_52h":  parse_num(f.get("52W High")),
    }


def _calc_rsi(closes, period=14):
    """Wilder's smoothed RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2)


def fetch_fundamentals_yfinance(ticker, yf_info=None):
    """Fetch fundamentals for international stocks via yfinance."""
    t    = yf.Ticker(ticker)
    info = yf_info if yf_info is not None else t.info

    if not info or not (info.get("shortName") or info.get("longName") or info.get("marketCap")):
        raise ValueError(f"No market data found for {ticker} — ticker may be delisted or incorrect")

    def pct(v):
        return round(v * 100, 2) if v is not None else None

    def cap(v, lo=None, hi=None):
        """Return None if value is outside plausible bounds (catches yfinance sentinel garbage)."""
        if v is None:
            return None
        if lo is not None and v < lo:
            return None
        if hi is not None and v > hi:
            return None
        return v

    market_cap_raw = info.get("marketCap")
    market_cap_raw = cap(market_cap_raw, 1e6, 2e13)  # $1M – $20T
    market_cap_b   = round(market_cap_raw / 1e9, 2) if market_cap_raw else None

    ev_raw    = info.get("enterpriseValue")
    ev_b      = ev_raw / 1e9 if ev_raw else None
    ev_ebitda = info.get("enterpriseToEbitda")

    net_debt_ebitda = None
    if ev_b and market_cap_b and ev_ebitda and ev_ebitda != 0:
        ebitda_b        = ev_b / ev_ebitda
        net_debt_ebitda = round((ev_b - market_cap_b) / ebitda_b, 2) if ebitda_b else None

    fcf       = info.get("freeCashflow")
    fcf_yield = round(fcf / market_cap_raw * 100, 2) if fcf and market_cap_raw else None

    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    target_price  = info.get("targetMeanPrice")

    # yfinance returns debtToEquity as a percentage (e.g. 176 = 1.76x) — normalise to ratio
    debt_eq_raw = info.get("debtToEquity")
    debt_eq = round(debt_eq_raw / 100, 2) if debt_eq_raw is not None else None

    analyst_recom_raw = info.get("recommendationMean")
    analyst_recom = str(round(analyst_recom_raw, 1)) if analyst_recom_raw is not None else None

    rsi = vs_sma50 = vs_sma200 = pct_from_52h = perf_month = perf_year = None
    try:
        hist   = t.history(period="14mo")
        closes = list(hist["Close"]) if not hist.empty else []
        n      = len(closes)
        if closes and current_price:
            rsi = _calc_rsi(closes)
            if n >= 50:
                sma50    = sum(closes[-50:]) / 50
                vs_sma50 = round((current_price - sma50) / sma50 * 100, 2)
            if n >= 200:
                sma200    = sum(closes[-200:]) / 200
                vs_sma200 = round((current_price - sma200) / sma200 * 100, 2)
            high_52w = info.get("fiftyTwoWeekHigh")
            if high_52w is None and n > 0:
                high_52w = max(closes[-252:] if n >= 252 else closes)
            if high_52w:
                pct_from_52h = round((current_price - high_52w) / high_52w * 100, 2)
            if n >= 22:
                perf_month = round((current_price - closes[-22]) / closes[-22] * 100, 2)
            if n >= 253:
                perf_year = round((current_price - closes[-253]) / closes[-253] * 100, 2)
            elif n > 1:
                perf_year = round((current_price - closes[0]) / closes[0] * 100, 2)
    except Exception:
        pass

    return {
        "ticker":             ticker.upper(),
        "company_name":       info.get("shortName") or info.get("longName") or ticker.upper(),
        "sector":             info.get("sector", ""),
        "industry":           info.get("industry", ""),
        "market_cap_b":       market_cap_b,
        "pe_ratio":           cap(info.get("trailingPE"),              0.1,  10000),
        "forward_pe":         cap(info.get("forwardPE"),               0.1,  10000),
        "ev_ebitda":          cap(ev_ebitda,                          -500,   2000),
        "ps_ratio":           cap(info.get("priceToSalesTrailing12Months"), 0, 10000),
        "pb_ratio":           cap(info.get("priceToBook"),               0,  10000),
        "gross_margin":       pct(info.get("grossMargins")),
        "operating_margin":   pct(info.get("operatingMargins")),
        "net_margin":         pct(info.get("profitMargins")),
        "roe":                pct(info.get("returnOnEquity")),
        "roic":               None,
        "revenue_growth_yoy": pct(info.get("revenueGrowth")),
        "eps_growth_yoy":     pct(info.get("earningsGrowth")),
        "current_ratio":      info.get("currentRatio"),
        "debt_eq":            debt_eq,
        "net_debt_ebitda":    net_debt_ebitda,
        "fcf_yield":          fcf_yield,
        "dividend_yield":     cap(pct(info.get("dividendYield")), 0, 30),
        "analyst_recom":      analyst_recom,
        "current_price":      current_price,
        "target_price":       target_price,
        "perf_year":          perf_year,
        "perf_month":         perf_month,
        "short_float":        pct(info.get("shortPercentOfFloat")),
        "short_ratio":        info.get("shortRatio"),
        "rsi":                rsi,
        "vs_sma50":           vs_sma50,
        "vs_sma200":          vs_sma200,
        "pct_from_52h":       pct_from_52h,
        "currency":           info.get("currency", "USD"),
        "exchange":           info.get("exchange", ""),
    }


def fetch_fundamentals_auto(ticker, use_yfinance=False):
    """Route to yfinance for international tickers (contain '.'), Finviz for US."""
    if use_yfinance or "." in ticker:
        return fetch_fundamentals_yfinance(ticker)
    try:
        result = fetch_fundamentals_finviz(ticker)
        result.setdefault("currency", "USD")
        return result
    except Exception:
        result = fetch_fundamentals_yfinance(ticker)
        result["currency"] = "USD"
        return result


def fetch_earnings_date(ticker):
    """Return next earnings date as 'MMM D, YYYY' string, or None if unavailable."""
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return None
        # calendar is a dict with 'Earnings Date' key containing a list of timestamps
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                dt = dates[0]
                if hasattr(dt, "strftime"):
                    return dt.strftime("%-d %b %Y")
        return None
    except Exception:
        return None


def short_sentiment(short_float, short_ratio):
    """Classify short interest into an actionable signal label."""
    if short_float is None:
        return "UNKNOWN"
    if short_float < 2:
        label = "LOW"
    elif short_float < 5:
        label = "NORMAL"
    elif short_float < 10:
        label = "ELEVATED"
    elif short_float < 20:
        label = "HIGH"
    else:
        label = "EXTREME"
    # Upgrade to squeeze candidate if days-to-cover is also high
    if short_ratio is not None and short_ratio >= 7 and short_float >= 10:
        label = "SQUEEZE CANDIDATE"
    return label


def resolve_ticker(query, api_key):
    """Resolve a company name or misspelled input to a stock ticker (US or international)."""
    client = anthropic.Anthropic(api_key=api_key, timeout=15.0)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=15,
        temperature=0,
        messages=[{
            "role": "user",
            "content": (
                f'What is the CURRENT primary stock exchange ticker for "{query}"? '
                f'Use the most up-to-date ticker — if the company has rebranded or renamed, use the new ticker. '
                f'For US companies return the US ticker (e.g. AAPL, NVDA). '
                f'For international companies return the primary listing ticker with exchange suffix. '
                f'Exchange suffixes by market: '
                f'.DE Germany, .L London/UK, .PA France, .MI Italy, .SW Switzerland, .AS Netherlands, .IR Ireland; '
                f'.T Japan, .HK Hong Kong, .SS Shanghai, .SZ Shenzhen, .KS South Korea, .TW Taiwan, .SI Singapore; '
                f'.TO Canada, .KL Malaysia, .JK Indonesia, .BK Thailand. '
                f'Examples: DHL.DE (Deutsche Post), HSBA.L (HSBC), MC.PA (LVMH), ENEL.MI (Enel), NESN.SW (Nestlé), BIRG.IR (Bank of Ireland), '
                f'7203.T (Toyota), 0700.HK (Tencent), 600519.SS (Kweichow Moutai), 005930.KS (Samsung), '
                f'2330.TW (TSMC), D05.SI (DBS Bank), RY.TO (Royal Bank of Canada), 1155.KL (Maybank). '
                f'Reply with ONLY the ticker symbol in uppercase. If genuinely no match exists, reply UNKNOWN.'
            ),
        }],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip().upper()
    if re.match(r'^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$', text):
        return text
    return None


def identify_peers(ticker, company_name, sector, industry, api_key):
    """Use Claude to identify 7 best-in-class publicly traded peers by business model."""
    client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        temperature=0,
        messages=[{
            "role": "user",
            "content": (
                f"List the 7 closest publicly traded competitors to {ticker} "
                f"({company_name}, {sector} / {industry}) by actual business model and revenue overlap. "
                f"For international stocks use exchange suffixes (e.g. DHL.DE, 0700.HK, SHEL.L). "
                f"Return ONLY a JSON array of ticker symbols, e.g. [\"AMD\",\"TSM\",\"ASML.AS\"]. "
                f"No explanation, no markdown, just the array."
            ),
        }],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if not match:
        return []
    tickers = json.loads(match.group(0))
    return [t.upper() for t in tickers if t.upper() != ticker.upper()][:7]


# ── Scoring engine ────────────────────────────────────────────────────────────

def rank_metric(pairs, lower_is_better=False):
    """
    Score each company 0-100 on a single metric using linear relative ranking.
    pairs: list of (ticker, value). None values receive a neutral 50.
    Rank 1 (best) = 100, Rank N (worst) = 0.
    """
    valid = [(t, v) for t, v in pairs if v is not None]
    n = len(valid)
    scores = {t: 50 for t, _ in pairs}  # default neutral for missing data
    if n < 2:
        if n == 1:
            scores[valid[0][0]] = 50
        return scores
    sorted_vals = sorted(valid, key=lambda x: x[1], reverse=not lower_is_better)
    for rank_idx, (t, _) in enumerate(sorted_vals):
        scores[t] = round(((n - 1 - rank_idx) / (n - 1)) * 100)
    return scores


def avg_scores(score_dicts, ticker):
    """Average a ticker's score across multiple metric score dicts."""
    vals = [d[ticker] for d in score_dicts if ticker in d]
    return round(sum(vals) / len(vals)) if vals else 50


def compute_scores(target, competitors):
    """
    Deterministic multi-factor relative ranking.
    Methodology mirrors institutional factor models (MSCI Quality, GS factor baskets):
      - Valuation  20%: P/E, Forward P/E, EV/EBITDA, P/S, P/B  (lower = better)
      - Profitability 30%: Gross Margin, Op Margin, Net Margin, ROIC, ROE (higher = better)
      - Growth     25%: Revenue Growth YoY, EPS Growth YoY       (higher = better)
      - Health     25%: Current Ratio (higher), Net Debt/EBITDA (lower), Debt/Eq (lower)
    """
    all_cos = [target] + competitors
    tickers = [c["ticker"] for c in all_cos]

    def pairs(field, sanitize=None):
        result = []
        for c in all_cos:
            v = c.get(field)
            if sanitize:
                v = sanitize(v)
            result.append((c["ticker"], v))
        return result

    # Negative P/E means losses — not comparable, treat as missing
    def positive_only(v):
        return v if (v is not None and v > 0) else None

    # Valuation (lower = better)
    val_scores = [
        rank_metric(pairs("pe_ratio",    positive_only), lower_is_better=True),
        rank_metric(pairs("forward_pe",  positive_only), lower_is_better=True),
        rank_metric(pairs("ev_ebitda"),                  lower_is_better=True),
        rank_metric(pairs("ps_ratio"),                   lower_is_better=True),
        rank_metric(pairs("pb_ratio"),                   lower_is_better=True),
    ]

    # Profitability (higher = better)
    prof_scores = [
        rank_metric(pairs("gross_margin")),
        rank_metric(pairs("operating_margin")),
        rank_metric(pairs("net_margin")),
        rank_metric(pairs("roic")),
        rank_metric(pairs("roe")),
    ]

    # Growth (higher = better)
    growth_scores = [
        rank_metric(pairs("revenue_growth_yoy")),
        rank_metric(pairs("eps_growth_yoy")),
    ]

    # Health (mixed directions)
    health_scores = [
        rank_metric(pairs("current_ratio"),     lower_is_better=False),
        rank_metric(pairs("net_debt_ebitda"),   lower_is_better=True),
        rank_metric(pairs("debt_eq"),           lower_is_better=True),
    ]

    results = {}
    for t in tickers:
        v = avg_scores(val_scores,    t)
        p = avg_scores(prof_scores,   t)
        g = avg_scores(growth_scores, t)
        h = avg_scores(health_scores, t)
        o = round(p * 0.30 + g * 0.25 + h * 0.25 + v * 0.20)
        results[t] = {"valuation": v, "profitability": p, "growth": g, "health": h, "overall": o}

    def rank_by(key):
        ranked = sorted(tickers, key=lambda t: (results[t][key], t == tgt), reverse=True)
        return {t: i + 1 for i, t in enumerate(ranked)}

    tgt = target["ticker"]
    n   = len(all_cos)
    return {
        "scores": {
            "valuation":       results[tgt]["valuation"],
            "profitability":   results[tgt]["profitability"],
            "growth":          results[tgt]["growth"],
            "financial_health":results[tgt]["health"],
            "overall":         results[tgt]["overall"],
        },
        "rankings": {
            "total_peers":        n,
            "overall_rank":       rank_by("overall")[tgt],
            "valuation_rank":     rank_by("valuation")[tgt],
            "profitability_rank": rank_by("profitability")[tgt],
            "growth_rank":        rank_by("growth")[tgt],
            "health_rank":        rank_by("health")[tgt],
        },
        "peer_scores": results,
    }


def extract_narrative_json(text):
    """Extract and parse JSON from Claude's narrative response."""
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if match:
            text = match.group(1)
    json_match = re.search(r"\{[\s\S]+\}", text)
    if json_match:
        text = json_match.group(0)
    return json.loads(text)


def validate_narrative(n):
    """Return True only if all required narrative keys are present and non-empty."""
    required_strs = ["verdict_rationale"]
    ic = n.get("investment_case", {})
    kr = n.get("key_risks", {})
    return (
        isinstance(ic, dict) and isinstance(ic.get("present"), list) and len(ic["present"]) > 0
        and isinstance(ic.get("forward"), list) and len(ic["forward"]) > 0
        and isinstance(kr, dict) and isinstance(kr.get("present"), list) and len(kr["present"]) > 0
        and isinstance(kr.get("forward"), list) and len(kr["forward"]) > 0
        and all(isinstance(n.get(k), str) and n[k].strip() for k in required_strs)
    )


def compute_verdict(overall_score, analyst_recom, current_price, target_price):
    """
    Composite verdict: 40% analyst consensus + 35% price target upside + 25% peer score.
    Each component normalised to 0-100 before weighting.
    Missing components are excluded and remaining weights are renormalised.
    """
    components = {}

    recom_val = parse_num(analyst_recom)
    if recom_val is not None:
        # Finviz Recom: 1.0 = Strong Buy, 5.0 = Sell → invert to 0-100
        components["consensus"] = (5 - recom_val) / 4 * 100

    if current_price and target_price:
        upside_pct = (target_price - current_price) / current_price * 100
        # ≥30% → 100, 0% → 50, ≤-15% → 0, linear between
        components["upside"] = max(0, min(100, (upside_pct + 15) / 45 * 100))

    if overall_score is not None:
        components["peer_score"] = overall_score

    if not components:
        return "HOLD", None

    raw_weights = {"consensus": 0.40, "upside": 0.35, "peer_score": 0.25}
    total_w = sum(raw_weights[k] for k in components)
    composite = round(sum(components[k] * raw_weights[k] for k in components) / total_w)

    if   composite >= 78: verdict = "STRONG BUY"
    elif composite >= 62: verdict = "BUY"
    elif composite >= 42: verdict = "HOLD"
    elif composite >= 28: verdict = "UNDERPERFORM"
    else:                 verdict = "AVOID"

    return verdict, composite


# ── Narrative prompt (Claude writes analysis only, no scoring) ────────────────

NARRATIVE_PROMPT = """You are a senior institutional equity analyst. Fundamental data and scores have already been calculated. Use ONLY the data provided — do not search for anything.
{market_context}
TARGET: {ticker}
COMPUTED SCORES: {scores_json}
SHORT INTEREST: short_float={short_float}%, days_to_cover={short_ratio}, signal={short_signal}
TECHNICAL TIMING: {timing_json}
FULL DATA: {data_json}

Write an institutional-grade narrative analysis. Factor in the short interest signal when assessing risk and opportunity. Return ONLY this JSON:

{{
  "investment_case": {{
    "present": ["string", ...],
    "forward": ["string", ...]
  }},
  "key_risks": {{
    "present": ["string", ...],
    "forward": ["string", ...]
  }},
  "verdict_rationale": "string",
  "timing_commentary": "string",
  "sector_percentile": integer
}}

Rules:
- investment_case.present: current strengths — what is true about the business TODAY (margins, balance sheet, competitive position, market share). Generate as many unique, data-backed points as warranted. Each point must be ONE concise sentence with the single most critical supporting metric or fact inline — no elaboration, no multi-clause sentences.
- investment_case.forward: bull catalysts — forward-looking opportunities and growth drivers. Generate as many unique points as warranted. No overlap with present. Each point must be ONE concise sentence with the single most critical supporting metric or fact inline.
- key_risks.present: current weaknesses — problems or vulnerabilities that exist NOW. Generate as many unique points as warranted. Each point must be ONE concise sentence with the single most critical supporting metric or fact inline.
- key_risks.forward: bear scenarios and specific risks — forward-looking downside risks, competitive threats, macro headwinds. Generate as many unique points as warranted. No overlap with present. Each point must be ONE concise sentence with the single most critical supporting metric or fact inline.
- verdict_rationale: 2-3 sentences referencing the computed scores and key metrics
- timing_commentary: exactly 1 sentence connecting the technical timing verdict to the fundamental story (e.g. whether the timing supports acting now or waiting for a better entry)
- sector_percentile: your estimate (0-100) of where {ticker} ranks in its broader sector universe"""


def get_valid_codes():
    raw = os.environ.get("INVITE_CODES", "")
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def check_invite_code(req):
    codes = get_valid_codes()
    if not codes:
        return True  # no codes configured = unrestricted
    code = req.headers.get("X-Invite-Code", "").strip().upper()
    return code in codes


@app.route("/api/search", methods=["GET"])
def search_tickers():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"quotes": []})
    url = (
        f"https://query2.finance.yahoo.com/v1/finance/search"
        f"?q={urllib.parse.quote(q)}&quotesCount=8&newsCount=0&enableFuzzyQuery=false&enableCb=false"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        quotes = [
            {k: r.get(k, "") for k in ("symbol", "shortname", "longname", "quoteType", "exchDisp", "exchange")}
            for r in data.get("quotes", [])
            if r.get("symbol") and r.get("quoteType")
        ]
        return jsonify({"quotes": quotes})
    except Exception:
        return jsonify({"quotes": []})


@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    if not code:
        return jsonify({"valid": False}), 400
    valid = code in get_valid_codes()
    return jsonify({"valid": valid})


@app.route("/api/test-key", methods=["GET"])
def test_key():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return jsonify({"status": "ok", "model": response.model})
    except Exception as e:
        logger.error("API key test failed: %s", e)
        return jsonify({"error": "API key test failed"}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not check_invite_code(request):
        return jsonify({"error": "Invalid or missing invite code."}), 403

    data = request.get_json(silent=True) or {}
    raw_input = data.get("ticker", "").strip()

    if not raw_input:
        return jsonify({"error": "Ticker symbol or company name is required"}), 400
    if len(raw_input) > 60:
        return jsonify({"error": "Input too long"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured on server"}), 500

    t0 = time.time()

    # Always resolve via Claude to get the correct current ticker
    ticker = resolve_ticker(raw_input, api_key)
    print(f"TIMING resolve_ticker: {time.time()-t0:.2f}s")
    if not ticker:
        return jsonify({"error": f'Could not identify a stock ticker for "{raw_input}". Try entering the ticker directly (e.g. NVDA or DHL.DE).'}), 400
    resolved_from = raw_input if ticker != raw_input.upper() else None

    # Return cached result if same ticker was analysed today
    cached = _ticker_cache.get(ticker)
    if cached and cached["date"] == date.today():
        return jsonify({**cached["result"], "cache_hit": True})

    # Start earnings + SPY fetches in background immediately — only need ticker
    _executor = ThreadPoolExecutor(max_workers=8)
    fut_earnings = _executor.submit(fetch_earnings_date, ticker)
    fut_spy      = _executor.submit(get_spy_perf)

    # Step 1: reject ETFs early; detect exchange for routing
    t1 = time.time()
    yf_info        = None
    is_international = False
    currency       = "USD"
    try:
        _yf_obj    = yf.Ticker(ticker)
        yf_info    = _yf_obj.info
        quote_type = yf_info.get("quoteType", "")
        if quote_type in ("ETF", "MUTUALFUND", "INDEX", "FUTURE", "CURRENCY"):
            _executor.shutdown(wait=False)
            return jsonify({"error": f"{ticker} is an {quote_type.lower() if quote_type != 'ETF' else 'ETF'}. EQA is designed for individual equities — try a stock ticker instead."}), 422
        exchange       = yf_info.get("exchange", "")
        is_international = bool(exchange) and exchange not in US_EXCHANGES
        currency       = yf_info.get("currency", "USD")
    except Exception:
        pass

    # Step 2: fetch target — any ticker with '.' always uses yfinance
    try:
        if is_international or "." in ticker:
            target = fetch_fundamentals_yfinance(ticker, yf_info=yf_info)
            is_international = True
        else:
            try:
                target = fetch_fundamentals_finviz(ticker)
            except Exception:
                target = fetch_fundamentals_yfinance(ticker, yf_info=yf_info)
                exch = target.get("exchange", "")
                is_international = bool(exch) and exch not in US_EXCHANGES
                currency = target.get("currency", "USD")
        target.setdefault("currency", currency)
        target.setdefault("is_international", is_international)
    except ValueError as e:
        _executor.shutdown(wait=False)
        return jsonify({"error": str(e) + ". Try entering the ticker directly (e.g. DHL.DE)."}), 404
    except Exception as e:
        _executor.shutdown(wait=False)
        logger.error("Fundamentals fetch failed for %s: %s", ticker, e)
        return jsonify({"error": f"Could not retrieve market data for {ticker}. Please try again."}), 502

    print(f"TIMING yf_info + target: {time.time()-t1:.2f}s")
    timing = compute_timing(target)

    # Step 3: Claude identifies best-in-class peers (one retry on failure)
    t2 = time.time()
    def _identify_peers_with_retry():
        try:
            return identify_peers(
                ticker,
                target.get("company_name", ticker),
                target.get("sector", ""),
                target.get("industry", ""),
                api_key,
            )
        except Exception:
            try:
                return identify_peers(
                    ticker,
                    target.get("company_name", ticker),
                    target.get("sector", ""),
                    target.get("industry", ""),
                    api_key,
                )
            except Exception:
                return []

    competitor_tickers = _identify_peers_with_retry()
    print(f"TIMING identify_peers: {time.time()-t2:.2f}s")

    # Step 4: fetch all peer fundamentals in parallel
    t3 = time.time()
    def _fetch_peer_safe(ct):
        try:
            return (ct, fetch_fundamentals_auto(ct, use_yfinance=is_international))
        except Exception:
            return (ct, None)

    peer_futures = {_executor.submit(_fetch_peer_safe, ct): ct for ct in competitor_tickers}
    competitors  = []
    peers_failed = []
    for fut in as_completed(peer_futures):
        ct, result = fut.result()
        if result is not None:
            competitors.append(result)
        else:
            peers_failed.append(ct)

    print(f"TIMING peer fetches ({len(competitor_tickers)} peers): {time.time()-t3:.2f}s")

    # Collect background results (almost certainly done by now)
    earnings_date = fut_earnings.result()
    spy_result    = fut_spy.result()
    print(f"TIMING earnings+spy background wait: {time.time()-t0:.2f}s")

    # Step 5: deterministic Python scoring + short sentiment
    sf           = target.get("short_float")
    sr           = target.get("short_ratio")
    short_signal = short_sentiment(sf, sr)

    if len(competitors) >= MIN_PEERS:
        scoring     = compute_scores(target, competitors)
        scores      = scoring["scores"]
        rankings    = scoring["rankings"]
        peer_scores = scoring["peer_scores"]
        verdict, verdict_composite = compute_verdict(
            scores["overall"],
            target.get("analyst_recom"),
            target.get("current_price"),
            target.get("target_price"),
        )
    else:
        scores            = None
        rankings          = {}
        peer_scores       = {}
        verdict           = "INSUFFICIENT DATA"
        verdict_composite = None

    _executor.shutdown(wait=False)
    spy = None if is_international else spy_result

    # Phase 1 payload — all Python-computed data, narrative fields empty
    base_result = {
        "_streaming":      True,
        "cache_hit":       False,
        "ticker":          target["ticker"],
        "company_name":    target["company_name"],
        "sector":          target["sector"],
        "industry":        target["industry"],
        "resolved_from":   resolved_from,
        "earnings_date":   earnings_date,
        "currency":        currency,
        "is_international": is_international,
        "current_price":   target.get("current_price"),
        "target_price":    target.get("target_price"),
        "perf_year":       target.get("perf_year"),
        "spy_perf_year":   spy,
        "vs_sp500":        round(target["perf_year"] - spy, 2) if target.get("perf_year") and spy else None,
        "short_float":     sf,
        "short_ratio":     sr,
        "short_signal":    short_signal,
        "fundamentals": {
            "market_cap_b":       target.get("market_cap_b"),
            "pe_ratio":           target.get("pe_ratio"),
            "forward_pe":         target.get("forward_pe"),
            "ev_ebitda":          target.get("ev_ebitda"),
            "ps_ratio":           target.get("ps_ratio"),
            "pb_ratio":           target.get("pb_ratio"),
            "gross_margin":       target.get("gross_margin"),
            "operating_margin":   target.get("operating_margin"),
            "net_margin":         target.get("net_margin"),
            "roic":               target.get("roic"),
            "roe":                target.get("roe"),
            "revenue_growth_yoy": target.get("revenue_growth_yoy"),
            "eps_growth_yoy":     target.get("eps_growth_yoy"),
            "net_debt_ebitda":    target.get("net_debt_ebitda"),
            "current_ratio":      target.get("current_ratio"),
            "fcf_yield":          target.get("fcf_yield"),
            "dividend_yield":     target.get("dividend_yield"),
        },
        "competitors": [
            {
                "ticker":             c.get("ticker"),
                "company_name":       c.get("company_name"),
                "market_cap_b":       c.get("market_cap_b"),
                "perf_year":          c.get("perf_year"),
                "pe_ratio":           c.get("pe_ratio"),
                "ev_ebitda":          c.get("ev_ebitda"),
                "ps_ratio":           c.get("ps_ratio"),
                "gross_margin":       c.get("gross_margin"),
                "operating_margin":   c.get("operating_margin"),
                "net_margin":         c.get("net_margin"),
                "roic":               c.get("roic"),
                "revenue_growth_yoy": c.get("revenue_growth_yoy"),
                "eps_growth_yoy":     c.get("eps_growth_yoy"),
                "net_debt_ebitda":    c.get("net_debt_ebitda"),
                "fcf_yield":          c.get("fcf_yield"),
            }
            for c in competitors
        ],
        "peers_found":      len(competitors),
        "peers_attempted":  competitor_tickers,
        "peers_failed":     peers_failed,
        "scores":           scores,
        "peer_scores":      peer_scores,
        "rankings":         {**rankings, "sector_percentile": 50} if rankings else {"sector_percentile": 50},
        "analyst_verdict":  verdict,
        "verdict_composite": verdict_composite,
        "timing":           timing,
        "investment_case":  {"present": [], "forward": []},
        "key_risks":        {"present": [], "forward": []},
        "verdict_rationale": "",
    }

    # Step 6: Claude narrative — stream phase 1 immediately, phase 2 when done
    narrative_data = {
        "target": {k: target.get(k) for k in (
            "ticker","company_name","sector","industry","market_cap_b",
            "pe_ratio","forward_pe","ev_ebitda","ps_ratio","gross_margin",
            "operating_margin","net_margin","roic","revenue_growth_yoy",
            "eps_growth_yoy","net_debt_ebitda","fcf_yield","perf_year",
            "target_price","analyst_recom",
        )},
        "competitors": [
            {k: c.get(k) for k in (
                "ticker","company_name","market_cap_b","perf_year","pe_ratio",
                "ev_ebitda","ps_ratio","gross_margin","operating_margin","net_margin",
                "roic","revenue_growth_yoy","eps_growth_yoy","net_debt_ebitda",
            )}
            for c in competitors
        ],
    }

    market_context = (
        f"MARKET CONTEXT: International stock listed on {target.get('exchange','non-US exchange')} "
        f"in {currency}. All financial metrics are reported in {currency}.\n"
        if is_international else ""
    )
    claude_client = anthropic.Anthropic(api_key=api_key, timeout=100.0)

    def _prompt_kwargs():
        return dict(
            market_context=market_context,
            ticker=ticker,
            scores_json=json.dumps(scores, indent=2),
            short_float=sf if sf is not None else "N/A",
            short_ratio=sr if sr is not None else "N/A",
            short_signal=short_signal,
            timing_json=json.dumps(timing, indent=2),
            data_json=json.dumps(narrative_data, indent=2),
        )

    def stream_response():
        # Phase 1: send Python-computed data immediately (~5s into request)
        yield f"data: {json.dumps(base_result)}\n\n"

        try:
            response = claude_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=3000,
                temperature=0,
                messages=[{"role": "user", "content": NARRATIVE_PROMPT.format(**_prompt_kwargs())}],
            )
            raw_text = next((b.text for b in response.content if b.type == "text"), "").strip()
            narrative = extract_narrative_json(raw_text)

            if not validate_narrative(narrative):
                retry = claude_client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=3000,
                    temperature=0,
                    messages=[{"role": "user", "content": NARRATIVE_PROMPT.format(**_prompt_kwargs())}],
                )
                retry_text = next((b.text for b in retry.content if b.type == "text"), "").strip()
                try:
                    narrative = extract_narrative_json(retry_text)
                except Exception:
                    pass

            print(f"TIMING Claude narrative: {time.time()-t0:.2f}s")

            phase2 = {
                "type":             "narrative",
                "investment_case":  narrative.get("investment_case", {"present": [], "forward": []}),
                "key_risks":        narrative.get("key_risks", {"present": [], "forward": []}),
                "verdict_rationale": narrative.get("verdict_rationale", ""),
                "timing_commentary": narrative.get("timing_commentary", ""),
                "sector_percentile": narrative.get("sector_percentile", 50),
            }
            yield f"data: {json.dumps(phase2)}\n\n"

            # Cache the complete merged result for same-day requests
            full_result = {
                **base_result,
                "_streaming":       False,
                "investment_case":  phase2["investment_case"],
                "key_risks":        phase2["key_risks"],
                "verdict_rationale": phase2["verdict_rationale"],
                "timing":           {**timing, "commentary": phase2["timing_commentary"]},
                "rankings":         {**rankings, "sector_percentile": phase2["sector_percentile"]} if rankings else {"sector_percentile": phase2["sector_percentile"]},
            }
            _ticker_cache[ticker] = {"date": date.today(), "result": full_result}
            print(f"TIMING total: {time.time()-t0:.2f}s")

        except Exception as e:
            logger.error("Narrative generation failed for %s: %s", ticker, e)
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(stream_response()),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
