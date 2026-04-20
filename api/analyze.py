import json
import os
import re

import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

SYSTEM_PROMPT = """You are a senior institutional equity analyst at a top-tier investment fund.
You have access to web search to find real, current financial data.
Search multiple sources to find accurate fundamental metrics.
You MUST return ONLY a valid JSON object. No markdown, no code blocks, no explanation, no preamble. Raw JSON only."""


def build_prompt(ticker: str) -> str:
    return f"""Perform institutional-grade fundamental analysis of {ticker.upper()}.

STEP 1: Search for {ticker.upper()} current fundamental data. Look for:
- Valuation: P/E, Forward P/E, EV/EBITDA, P/S, P/B ratios
- Profitability: gross margin, operating margin, net margin, ROIC, ROE
- Growth: revenue growth YoY, EPS growth YoY
- Balance sheet: net debt/EBITDA, current ratio, interest coverage
- Cash flow: FCF yield, dividend yield
- Market cap (in billions USD)

STEP 2: Identify 4-5 key publicly traded competitors in the same sector/industry.

STEP 3: Search for each competitor's fundamental data (same metrics where available).

STEP 4: Score {ticker.upper()} on each dimension relative to peers (0-100 scale):
- Valuation (100 = cheapest vs peers, 0 = most expensive)
- Profitability (100 = highest margins/ROIC vs peers)
- Growth (100 = fastest growing vs peers)
- Financial Health (100 = strongest balance sheet/FCF vs peers)
- Overall = weighted average (profitability 30%, growth 25%, health 25%, valuation 20%)

STEP 5: Rank {ticker.upper()} against all peers (1 = best overall).

Return ONLY this JSON (use null for unavailable data, all percentages as floats e.g. 44.5 for 44.5%):

{{
  "ticker": "{ticker.upper()}",
  "company_name": "string",
  "sector": "string",
  "industry": "string",
  "data_as_of": "string",
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
    "net_debt_ebitda": float,
    "current_ratio": float,
    "fcf_yield": float,
    "interest_coverage": float,
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
      "net_debt_ebitda": float,
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
  "bull_case": "string (2-3 sentences)",
  "bear_case": "string (2-3 sentences)",
  "analyst_verdict": "STRONG BUY | BUY | HOLD | UNDERPERFORM | AVOID",
  "verdict_rationale": "string (2-3 sentences)"
}}"""


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

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": build_prompt(ticker)}],
        )

        text_parts = [block.text for block in response.content if block.type == "text"]
        full_text = " ".join(text_parts).strip()

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
        preview = full_text[:300] if "full_text" in dir() else "no response"
        return jsonify({"error": f"Failed to parse response: {str(e)}", "preview": preview}), 500
    except anthropic.APIError as e:
        return jsonify({"error": f"Anthropic API error: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
