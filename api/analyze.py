import json
import logging
import os
import re
import time
from datetime import date

logger = logging.getLogger(__name__)

import anthropic
import yfinance as yf
from finvizfinance.quote import finvizfinance as fvf
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CLAUDE_MODEL      = "claude-sonnet-4-6"  # narrative + peer identification
CLAUDE_MODEL_FAST = "claude-haiku-4-5"   # ticker resolution only

_spy_cache = {"perf_year": None, "ts": 0}
_SPY_TTL = 86400  # 24 hours

_ticker_cache = {}  # {ticker: {"date": date, "result": dict}}


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


def fetch_fundamentals(ticker):
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
        "target_price": parse_num(f.get("Target Price")),
        "perf_year": parse_num(f.get("Perf Year")),
        "short_float": parse_num(f.get("Short Float")),
        "short_ratio": parse_num(f.get("Short Ratio")),
    }


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
    """Resolve a company name or misspelled input to a valid US ticker symbol."""
    client = anthropic.Anthropic(api_key=api_key, timeout=15.0)
    response = client.messages.create(
        model=CLAUDE_MODEL_FAST,
        max_tokens=10,
        temperature=0,
        messages=[{
            "role": "user",
            "content": (
                f'What is the primary US stock exchange ticker symbol for "{query}"? '
                f'Reply with ONLY the ticker symbol in uppercase. If unknown, reply UNKNOWN.'
            ),
        }],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip().upper()
    if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', text):
        return text
    return None


def identify_peers(ticker, company_name, sector, industry, api_key):
    """Use Claude to identify 7 best-in-class publicly traded peers by business model."""
    client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=150,
        temperature=0,
        messages=[{
            "role": "user",
            "content": (
                f"List the 7 closest publicly traded competitors to {ticker} "
                f"({company_name}, {sector} / {industry}) by actual business model and revenue overlap. "
                f"Return ONLY a JSON array of uppercase ticker symbols, e.g. [\"AMD\",\"INTC\",\"QCOM\",\"AVGO\",\"TSM\"]. "
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
        ranked = sorted(tickers, key=lambda t: results[t][key], reverse=True)
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
    required_lists = ["strengths", "weaknesses", "key_risks", "bull_case", "bear_case"]
    required_strs  = ["verdict_rationale"]
    return (
        all(isinstance(n.get(k), list) and len(n[k]) > 0 for k in required_lists)
        and all(isinstance(n.get(k), str) and n[k].strip() for k in required_strs)
    )


def score_to_verdict(overall):
    if overall >= 80: return "STRONG BUY"
    if overall >= 65: return "BUY"
    if overall >= 45: return "HOLD"
    if overall >= 25: return "UNDERPERFORM"
    return "AVOID"


# ── Narrative prompt (Claude writes analysis only, no scoring) ────────────────

NARRATIVE_PROMPT = """You are a senior institutional equity analyst. Fundamental data and scores have already been calculated. Use ONLY the data provided — do not search for anything.

TARGET: {ticker}
COMPUTED SCORES: {scores_json}
SHORT INTEREST: short_float={short_float}%, days_to_cover={short_ratio}, signal={short_signal}
FULL DATA: {data_json}

Write an institutional-grade narrative analysis. Factor in the short interest signal when assessing risk and opportunity. Return ONLY this JSON:

{{
  "strengths": ["string", "string", "string"],
  "weaknesses": ["string", "string", "string"],
  "key_risks": ["string", "string", "string"],
  "bull_case": ["string", "string", "string"],
  "bear_case": ["string", "string", "string"],
  "verdict_rationale": "string",
  "sector_percentile": integer
}}

Rules:
- strengths/weaknesses/bull_case/bear_case: 3 specific, data-backed points each
- key_risks: 3 concrete risks with potential impact
- verdict_rationale: 2-3 sentences referencing the computed scores and key metrics
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

    # If input already looks like a ticker use it directly, otherwise resolve via Claude
    normalized = raw_input.upper()
    if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', normalized):
        ticker = normalized
        resolved_from = None
    else:
        ticker = resolve_ticker(raw_input, api_key)
        if not ticker:
            return jsonify({"error": f'Could not identify a stock ticker for "{raw_input}". Try entering the ticker directly (e.g. NVDA).'}), 400
        resolved_from = raw_input

    # Return cached result if same ticker was analysed today
    cached = _ticker_cache.get(ticker)
    if cached and cached["date"] == date.today():
        return jsonify({**cached["result"], "cache_hit": True})

    # Step 1: fetch target
    try:
        target = fetch_fundamentals(ticker)
    except Exception as e:
        logger.error("Finviz fetch failed for %s: %s", ticker, e)
        return jsonify({"error": f"Could not retrieve market data for {ticker}. Please try again."}), 502

    earnings_date = fetch_earnings_date(ticker)

    # Step 2: Claude identifies best-in-class peers (one retry on failure)
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

    # Step 3: fetch peer fundamentals
    competitors = []
    for ct in competitor_tickers:
        try:
            competitors.append(fetch_fundamentals(ct))
        except Exception:
            continue

    # Step 4: deterministic Python scoring + short sentiment
    # Require at least 3 peers for meaningful relative scoring
    MIN_PEERS = 3
    sf           = target.get("short_float")
    sr           = target.get("short_ratio")
    short_signal = short_sentiment(sf, sr)

    if len(competitors) >= MIN_PEERS:
        scoring     = compute_scores(target, competitors)
        scores      = scoring["scores"]
        rankings    = scoring["rankings"]
        peer_scores = scoring["peer_scores"]
        verdict     = score_to_verdict(scores["overall"])
    else:
        scores      = None
        rankings    = {}
        peer_scores = {}
        verdict     = "INSUFFICIENT DATA"

    # Step 5: Claude writes narrative only
    all_data = {"target": target, "competitors": competitors}
    client = anthropic.Anthropic(api_key=api_key, timeout=100.0)

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            temperature=0,
            messages=[{
                "role": "user",
                "content": NARRATIVE_PROMPT.format(
                    ticker=ticker,
                    scores_json=json.dumps(scores, indent=2),
                    short_float=sf if sf is not None else "N/A",
                    short_ratio=sr if sr is not None else "N/A",
                    short_signal=short_signal,
                    data_json=json.dumps(all_data, indent=2),
                ),
            }],
        )

        raw_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        ).strip()
        narrative = extract_narrative_json(raw_text)

        # Retry once if narrative is missing required keys
        if not validate_narrative(narrative):
            retry = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=3000,
                temperature=0,
                messages=[{
                    "role": "user",
                    "content": NARRATIVE_PROMPT.format(
                        ticker=ticker,
                        scores_json=json.dumps(scores, indent=2),
                        short_float=sf if sf is not None else "N/A",
                        short_ratio=sr if sr is not None else "N/A",
                        short_signal=short_signal,
                        data_json=json.dumps(all_data, indent=2),
                    ),
                }],
            )
            retry_text = next(
                (b.text for b in retry.content if b.type == "text"), ""
            ).strip()
            try:
                narrative = extract_narrative_json(retry_text)
            except Exception:
                pass  # keep original partial narrative, banner will flag missing sections

        # Build final result: Python data + Python scores + Claude narrative
        spy = get_spy_perf()
        result = {
            "ticker":        target["ticker"],
            "company_name":  target["company_name"],
            "sector":        target["sector"],
            "industry":      target["industry"],
            "data_as_of":    "Latest (Finviz)",
            "resolved_from": resolved_from,
            "earnings_date": earnings_date,
            "current_price": target.get("current_price"),
            "target_price":  target.get("target_price"),
            "perf_year":     target.get("perf_year"),
            "spy_perf_year": spy,
            "vs_sp500":      round(target["perf_year"] - spy, 2) if target.get("perf_year") and spy else None,
            "short_float":   sf,
            "short_ratio":   sr,
            "short_signal":  short_signal,
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
                "interest_coverage":  None,
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
            "peers_found": len(competitors),
            "scores":      scores,
            "peer_scores": peer_scores,
            "rankings":    {**rankings, "sector_percentile": narrative.get("sector_percentile", 50)} if rankings else {"sector_percentile": narrative.get("sector_percentile", 50)},
            "analyst_verdict":  verdict,
            "strengths":        narrative.get("strengths", []),
            "weaknesses":       narrative.get("weaknesses", []),
            "key_risks":        narrative.get("key_risks", []),
            "bull_case":        narrative.get("bull_case", []),
            "bear_case":        narrative.get("bear_case", []),
            "verdict_rationale": narrative.get("verdict_rationale", ""),
        }
        result["cache_hit"] = False
        _ticker_cache[ticker] = {"date": date.today(), "result": result}
        return jsonify(result)

    except json.JSONDecodeError as e:
        logger.error("Narrative JSON parse failed for %s: %s", ticker, e)
        return jsonify({"error": "Analysis response could not be parsed. Please try again."}), 500
    except anthropic.APIError as e:
        logger.error("Anthropic API error for %s: %s", ticker, e)
        return jsonify({"error": "AI analysis service is temporarily unavailable. Please try again."}), 502
    except Exception as e:
        logger.error("Unexpected error for %s: %s", ticker, e)
        return jsonify({"error": "An unexpected error occurred. Please try again."}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
