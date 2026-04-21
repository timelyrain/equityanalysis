import json
import os
import re

import anthropic
from finvizfinance.quote import finvizfinance as fvf
from finvizfinance.screener.overview import Overview
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


def parse_num(val):
    """Convert Finviz string values to float, return None if unavailable."""
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
    """Fetch fundamental data from Finviz for a given ticker."""
    stock = fvf(ticker)
    f = stock.ticker_fundament()

    market_cap_raw = parse_num(f.get("Market Cap"))
    market_cap_b = round(market_cap_raw / 1e9, 2) if market_cap_raw else None

    return {
        "ticker": ticker.upper(),
        "company_name": f.get("Company", ticker.upper()),
        "sector": f.get("Sector", ""),
        "industry": f.get("Industry", ""),
        "market_cap_b": market_cap_b,
        "pe_ratio": parse_num(f.get("P/E")),
        "forward_pe": parse_num(f.get("Forward P/E")),
        "ev_ebitda": parse_num(f.get("EV/EBITDA")),
        "ps_ratio": parse_num(f.get("P/S")),
        "pb_ratio": parse_num(f.get("P/B")),
        "gross_margin": parse_num(f.get("Gross Margin")),
        "operating_margin": parse_num(f.get("Operating Margin")),
        "net_margin": parse_num(f.get("Net Margin")),
        "roe": parse_num(f.get("ROE")),
        "roic": parse_num(f.get("ROI")),
        "revenue_growth_yoy": parse_num(f.get("Sales Y/Y TTM")),
        "eps_growth_yoy": parse_num(f.get("EPS Y/Y TTM")),
        "current_ratio": parse_num(f.get("Current Ratio")),
        "debt_eq": parse_num(f.get("Debt/Eq")),
        "dividend_yield": parse_num(f.get("Dividend %")),
        "pfcf": parse_num(f.get("P/FCF")),
        "analyst_recom": f.get("Recom"),
    }


def fetch_competitors(ticker, industry, sector, limit=3):
    """Find top competitors by market cap in the same industry."""
    try:
        screener = Overview()
        screener.set_filter(filters_dict={"Industry": industry})
        df = screener.screener_view()
        if df is None or df.empty:
            screener.set_filter(filters_dict={"Sector": sector})
            df = screener.screener_view()
        if df is None or df.empty:
            return []

        if "Market Cap" in df.columns:
            df["_mc"] = df["Market Cap"].apply(parse_num)
            df = df.sort_values("_mc", ascending=False)
        tickers = [t for t in df["Ticker"].tolist() if t.upper() != ticker.upper()]
        return tickers[:limit]
    except Exception:
        return []


ANALYSIS_PROMPT = """You are a senior institutional equity analyst. You have been given pre-fetched fundamental data from Finviz. Use ONLY this data — do not search for anything.

TARGET: {ticker}
DATA:
{data_json}

Your task:
1. Score {ticker} on each dimension vs peers (0-100 scale):
   - Valuation: 100 = cheapest, 0 = most expensive (use P/E, Forward P/E, EV/EBITDA, P/S, P/B)
   - Profitability: 100 = best margins/ROIC/ROE vs peers
   - Growth: 100 = fastest revenue/EPS growth vs peers
   - Financial Health: 100 = strongest balance sheet (low debt, high current ratio)
   - Overall = weighted avg (profitability 30%, growth 25%, health 25%, valuation 20%)
2. Rank {ticker} among all companies (1 = best). Estimate sector_percentile (0-100).
3. Write 3 strengths, 3 weaknesses, 2 key risks, bull case, bear case, analyst verdict.

Return ONLY this JSON (null for missing data, percentages as floats e.g. 44.5):

{{
  "ticker": "{ticker}",
  "company_name": "string",
  "sector": "string",
  "industry": "string",
  "data_as_of": "Latest (Finviz)",
  "fundamentals": {{
    "market_cap_b": float,
    "pe_ratio": float,
    "forward_pe": float,
    "ev_ebitda": float,
    "ps_ratio": float,
    "pb_ratio": float,
    "gross_margin": float,
    "operating_margin": float,
    "net_margin": float,
    "roic": float,
    "roe": float,
    "revenue_growth_yoy": float,
    "eps_growth_yoy": float,
    "net_debt_ebitda": null,
    "current_ratio": float,
    "fcf_yield": float,
    "interest_coverage": null,
    "dividend_yield": float
  }},
  "competitors": [
    {{
      "ticker": "string",
      "company_name": "string",
      "market_cap_b": float,
      "pe_ratio": float,
      "ev_ebitda": float,
      "ps_ratio": float,
      "gross_margin": float,
      "operating_margin": float,
      "net_margin": float,
      "roic": float,
      "revenue_growth_yoy": float,
      "eps_growth_yoy": float,
      "net_debt_ebitda": null,
      "fcf_yield": float
    }}
  ],
  "scores": {{
    "valuation": integer,
    "profitability": integer,
    "growth": integer,
    "financial_health": integer,
    "overall": integer
  }},
  "rankings": {{
    "overall_rank": integer,
    "total_peers": integer,
    "valuation_rank": integer,
    "profitability_rank": integer,
    "growth_rank": integer,
    "health_rank": integer,
    "sector_percentile": integer
  }},
  "strengths": ["string", "string", "string"],
  "weaknesses": ["string", "string", "string"],
  "key_risks": ["string", "string"],
  "bull_case": "string",
  "bear_case": "string",
  "analyst_verdict": "STRONG BUY | BUY | HOLD | UNDERPERFORM | AVOID",
  "verdict_rationale": "string"
}}"""


@app.route("/api/test-key", methods=["GET"])
def test_key():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return jsonify({"status": "ok", "model": response.model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    ticker = data.get("ticker", "").strip().upper()

    if not ticker:
        return jsonify({"error": "Ticker symbol is required"}), 400
    if not ticker.isalpha() or len(ticker) > 6:
        return jsonify({"error": "Invalid ticker format"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured on server"}), 500

    try:
        target = fetch_fundamentals(ticker)
    except Exception as e:
        return jsonify({"error": f"Finviz data error for {ticker}: {str(e)}"}), 502

    competitor_tickers = fetch_competitors(
        ticker, target.get("industry", ""), target.get("sector", "")
    )

    competitors = []
    for ct in competitor_tickers:
        try:
            competitors.append(fetch_fundamentals(ct))
        except Exception:
            continue

    all_data = {"target": target, "competitors": competitors}

    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": ANALYSIS_PROMPT.format(
                    ticker=ticker,
                    data_json=json.dumps(all_data, indent=2),
                ),
            }],
        )

        full_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        ).strip()

        if "```" in full_text:
            match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", full_text)
            if match:
                full_text = match.group(1)

        json_match = re.search(r"\{[\s\S]+\}", full_text)
        if json_match:
            full_text = json_match.group(0)

        result = json.loads(full_text)
        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse response: {str(e)}"}), 500
    except anthropic.APIError as e:
        return jsonify({"error": f"Anthropic API error: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
