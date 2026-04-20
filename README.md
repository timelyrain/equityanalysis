# StockIQ — Institutional Equity Analysis Dashboard

Fundamental scoring, peer ranking, and sector benchmarking powered by Claude + live web search.

---

## Stack

- **Backend**: Flask (Python) → Anthropic API with web search
- **Frontend**: Vanilla HTML/CSS/JS — no framework dependencies
- **Hosting**: Vercel (Python serverless function + static frontend)

---

## Project Structure

```
stockiq/
├── api/
│   └── analyze.py       # Flask backend — Anthropic API integration
├── public/
│   └── index.html       # Dashboard frontend
├── requirements.txt
├── vercel.json
├── .env.example
└── README.md
```

---

## Local Development

### 1. Clone and install

```bash
git clone https://github.com/your-username/stockiq.git
cd stockiq
pip install -r requirements.txt
```

### 2. Set your API key

```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

### 3. Run the backend

```bash
cd api
ANTHROPIC_API_KEY=your_key_here python analyze.py
```

Backend runs at `http://localhost:5000`.

### 4. Serve the frontend

Open `public/index.html` in a browser, or use a local static server:

```bash
cd public
python -m http.server 3000
```

> **Note**: The frontend calls `/api/analyze`. For local dev you need to either:
> - Update the `API_URL` in `index.html` to `http://localhost:5000/api/analyze`, or
> - Use the Vercel CLI (`vercel dev`) which handles routing automatically.

---

## Deploy to Vercel

### Requirements
- Vercel account (Pro plan recommended — analysis takes 30–90s, Hobby limit is 60s)
- Anthropic API key with web search access

### Steps

```bash
# Install Vercel CLI
npm i -g vercel

# Login
vercel login

# Deploy from project root
vercel

# Set your environment variable
vercel env add ANTHROPIC_API_KEY
# → paste your key when prompted
# → select: Production, Preview, Development

# Redeploy to apply env vars
vercel --prod
```

Your app will be live at `https://your-project.vercel.app`.

---

## How It Works

1. User enters a ticker (e.g. `NVDA`)
2. Frontend POSTs to `/api/analyze`
3. Backend calls Claude Sonnet with web search enabled
4. Claude searches for live fundamental data across financial sources
5. Claude identifies 4–5 sector peers and searches their data
6. Claude scores and ranks the stock across 4 dimensions:
   - **Valuation** (lower multiples vs peers = higher score)
   - **Profitability** (higher margins and ROIC = higher score)
   - **Growth** (revenue + EPS growth vs peers)
   - **Financial Health** (balance sheet strength + FCF)
7. Dashboard renders with full peer comparison table, score rings, bull/bear cases, and verdict

---

## Scoring System

| Score | Interpretation |
|-------|---------------|
| 80–100 | Top quartile vs peers |
| 60–79  | Above median |
| 40–59  | Near median |
| 20–39  | Below median |
| 0–19   | Bottom quartile vs peers |

Overall score = weighted average:
- Profitability 30% · Growth 25% · Financial Health 25% · Valuation 20%

---

## Extending the Dashboard

- **Add Finviz data**: If you have Finviz Elite, you can export CSVs and build a local ingestion pipeline to replace the web search step with structured data.
- **Add DCF module**: Extend the prompt to return a DCF value estimate alongside the scoring.
- **Add watchlist**: Use browser localStorage to save analyzed tickers.
- **Add charting**: Integrate lightweight-charts.js for price overlays.

---

## Disclaimer

This tool is for research and educational purposes only. It is not financial advice. Always conduct your own due diligence before making investment decisions.
