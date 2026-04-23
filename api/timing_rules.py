"""
Technical timing rules — edit the threshold constants to tune signal sensitivity.
Edit RULES to add, remove, or reweight signals.
Redeploy after any change.

Rule format
-----------
Threshold rule : {"group", "signal", "op", "value",       "points", "label"}
Range rule     : {"group", "signal", "op":"range", "low", "high",   "points", "label"}

Operators: "lt", "lte", "gt", "gte", "eq", "range"

Within each group, rules are evaluated top-to-bottom and the FIRST match fires.
This creates mutually exclusive tiers — order extreme cases before mild ones.
If no rule fires for a signal the group contributes 0 points.
"""

# ── RSI thresholds ─────────────────────────────────────────────────────────────
RSI_OVERSOLD         = 32    # below → strong bullish (+2)
RSI_RECOVERING       = 45    # [32–45) → mildly bullish (+1)
RSI_MILD_OB          = 65    # [65–70) → mildly bearish (−1)
RSI_OVERBOUGHT       = 70    # above → strong bearish (−2)

# ── 50-day MA thresholds ───────────────────────────────────────────────────────
SMA50_EXTENDED       =  8.0  # % above SMA50 where move is stretched (−1)
SMA50_BROKEN         = -3.0  # % below SMA50 where near-term trend is broken (−1)
                              # [0, SMA50_EXTENDED) → healthy uptrend (+1)

# ── 52-week high proximity (pct_from_52h is negative; e.g. −8 = 8% below high) ─
DIST_52H_RESISTANCE  =  -5.0 # closer than 5% to 52W high → near resistance (−1)
DIST_52H_RECOVERY    = -25.0 # 25–45% below → potential recovery zone (+1)
DIST_52H_DOWNTREND   = -45.0 # deeper than 45% below → extended downtrend (−1)

# ── Analyst target upside thresholds ──────────────────────────────────────────
UPSIDE_STRONG        = 25.0  # >25% → strong conviction (+2)
UPSIDE_GOOD          = 15.0  # 15–25% → reasonable upside (+1)
UPSIDE_WEAK          =  5.0  # 5–15% → neutral (0)
UPSIDE_NEGATIVE      =  0.0  # 0–5% → largely priced in (−1); <0 → above target (−2)

# ── Monthly momentum thresholds ────────────────────────────────────────────────
PERF_MONTH_CHASING   =  8.0  # up >8% this month → avoid chasing (−1)
PERF_MONTH_DIP       = -5.0  # down 5–15% → dip opportunity (+1)
PERF_MONTH_STEEP     = -15.0 # down >15% → fires instead of mild dip rule for steeper declines (+1)

# ── Verdict score thresholds ───────────────────────────────────────────────────
SCORE_STRONG_ENTRY   =  4    # score ≥ 4
SCORE_GOOD_ENTRY     =  2    # score ≥ 2
SCORE_NEUTRAL_MIN    = -1    # score ≥ −1  (−1, 0, 1 → NEUTRAL)
SCORE_WAIT_MIN       = -4    # score ≥ −4  (−4, −3, −2 → WAIT FOR PULLBACK)
                              # score < −4  → CAUTION

# ── Scoring rules ──────────────────────────────────────────────────────────────

RULES = [

    # RSI — tiered, first match wins within group
    {"group": "rsi", "signal": "rsi", "op": "lt",    "value": RSI_OVERSOLD,
     "points": +2, "label": "Oversold RSI — reversal opportunity"},
    {"group": "rsi", "signal": "rsi", "op": "gte",   "value": RSI_OVERBOUGHT,
     "points": -2, "label": "Overbought RSI — elevated pullback risk"},
    {"group": "rsi", "signal": "rsi", "op": "range", "low": RSI_OVERSOLD,   "high": RSI_RECOVERING,
     "points": +1, "label": "RSI recovering from oversold"},
    {"group": "rsi", "signal": "rsi", "op": "range", "low": RSI_MILD_OB,    "high": RSI_OVERBOUGHT,
     "points": -1, "label": "RSI approaching overbought"},
    # RSI 45–65 → neutral, no rule needed

    # 200-day MA — long-term trend bias
    {"group": "sma200", "signal": "vs_sma200", "op": "gte", "value": 0,
     "points": +2, "label": "Above 200-day MA — long-term uptrend intact"},
    {"group": "sma200", "signal": "vs_sma200", "op": "lt",  "value": 0,
     "points": -2, "label": "Below 200-day MA — long-term downtrend"},

    # 50-day MA — medium-term trend and extension
    {"group": "sma50", "signal": "vs_sma50", "op": "gt",    "value": SMA50_EXTENDED,
     "points": -1, "label": f">{SMA50_EXTENDED:.0f}% extended above 50-day MA — stretched entry"},
    {"group": "sma50", "signal": "vs_sma50", "op": "lt",    "value": SMA50_BROKEN,
     "points": -1, "label": "Broken below 50-day MA — near-term trend weakening"},
    {"group": "sma50", "signal": "vs_sma50", "op": "range", "low": 0, "high": SMA50_EXTENDED,
     "points": +1, "label": "Healthy position above 50-day MA"},

    # Golden / death cross (derived signal: SMA50 > SMA200 when vs_sma50 < vs_sma200)
    {"group": "cross", "signal": "golden_cross", "op": "eq", "value": True,
     "points": +1, "label": "50/200-day golden cross — bullish MA structure"},
    {"group": "cross", "signal": "golden_cross", "op": "eq", "value": False,
     "points": -1, "label": "50/200-day death cross — bearish MA structure"},

    # 52-week high proximity
    {"group": "52wh", "signal": "pct_from_52h", "op": "gt",    "value": DIST_52H_RESISTANCE,
     "points": -1, "label": "Near 52-week high — limited near-term upside"},
    {"group": "52wh", "signal": "pct_from_52h", "op": "lt",    "value": DIST_52H_DOWNTREND,
     "points": -1, "label": "Deep below 52-week high — extended downtrend"},
    {"group": "52wh", "signal": "pct_from_52h", "op": "range", "low": DIST_52H_RECOVERY, "high": DIST_52H_RESISTANCE,
     "points": +1, "label": "Meaningful pullback from highs — potential recovery entry"},
    # [DIST_52H_DOWNTREND, DIST_52H_RECOVERY) → neutral, no rule needed

    # Analyst target upside
    {"group": "upside", "signal": "target_upside_pct", "op": "gte",   "value": UPSIDE_STRONG,
     "points": +2, "label": f">{UPSIDE_STRONG:.0f}% analyst target upside — strong conviction"},
    {"group": "upside", "signal": "target_upside_pct", "op": "lt",    "value": UPSIDE_NEGATIVE,
     "points": -2, "label": "Trading above analyst consensus target"},
    {"group": "upside", "signal": "target_upside_pct", "op": "range", "low": UPSIDE_GOOD,     "high": UPSIDE_STRONG,
     "points": +1, "label": "Solid analyst target upside remaining"},
    {"group": "upside", "signal": "target_upside_pct", "op": "range", "low": UPSIDE_NEGATIVE, "high": UPSIDE_WEAK,
     "points": -1, "label": "Analyst target upside <5% — largely priced in"},
    # [UPSIDE_WEAK, UPSIDE_STRONG) → neutral, no rule needed

    # Monthly momentum
    {"group": "momentum", "signal": "perf_month", "op": "gte",   "value": PERF_MONTH_CHASING,
     "points": -1, "label": f"Up {PERF_MONTH_CHASING:.0f}%+ this month — avoid chasing"},
    {"group": "momentum", "signal": "perf_month", "op": "lt",    "value": PERF_MONTH_STEEP,
     "points": +1, "label": "Steep monthly decline — oversold dip opportunity"},
    {"group": "momentum", "signal": "perf_month", "op": "range", "low": PERF_MONTH_DIP, "high": PERF_MONTH_CHASING,
     "points":  0, "label": None},  # neutral band — blocks steep-dip rule from matching mild declines
    {"group": "momentum", "signal": "perf_month", "op": "range", "low": PERF_MONTH_STEEP, "high": PERF_MONTH_DIP,
     "points": +1, "label": "Monthly pullback — potential dip entry"},
]


def compute_timing(target_data):
    """
    Compute technical timing verdict from target fundamentals data.
    Returns dict: { verdict, score, reasons (up to 3 driving signals) }
    """
    vs_sma50  = target_data.get("vs_sma50")
    vs_sma200 = target_data.get("vs_sma200")
    cur_price = target_data.get("current_price")
    tgt_price = target_data.get("target_price")

    target_upside_pct = (
        round((tgt_price - cur_price) / cur_price * 100, 1)
        if cur_price and tgt_price else None
    )
    golden_cross = (
        (vs_sma50 < vs_sma200)
        if vs_sma50 is not None and vs_sma200 is not None else None
    )

    signals = {
        "rsi":               target_data.get("rsi"),
        "vs_sma50":          vs_sma50,
        "vs_sma200":         vs_sma200,
        "pct_from_52h":      target_data.get("pct_from_52h"),
        "perf_month":        target_data.get("perf_month"),
        "target_upside_pct": target_upside_pct,
        "golden_cross":      golden_cross,
    }

    fired_groups = set()
    score   = 0
    reasons = []

    for rule in RULES:
        group = rule["group"]
        if group in fired_groups:
            continue

        sig_val = signals.get(rule["signal"])
        if sig_val is None:
            continue

        op      = rule["op"]
        matched = False
        if   op == "lt"    and sig_val <  rule["value"]:                     matched = True
        elif op == "lte"   and sig_val <= rule["value"]:                     matched = True
        elif op == "gt"    and sig_val >  rule["value"]:                     matched = True
        elif op == "gte"   and sig_val >= rule["value"]:                     matched = True
        elif op == "eq"    and sig_val == rule["value"]:                     matched = True
        elif op == "range" and rule["low"] <= sig_val < rule["high"]:        matched = True

        if matched:
            fired_groups.add(group)
            score += rule["points"]
            if rule.get("label"):
                reasons.append(rule["label"])

    if   score >= SCORE_STRONG_ENTRY: verdict = "STRONG ENTRY"
    elif score >= SCORE_GOOD_ENTRY:   verdict = "GOOD ENTRY"
    elif score >= SCORE_NEUTRAL_MIN:  verdict = "NEUTRAL"
    elif score >= SCORE_WAIT_MIN:     verdict = "WAIT FOR PULLBACK"
    else:                             verdict = "CAUTION"

    return {"verdict": verdict, "score": score, "reasons": reasons[:3]}
