#!/usr/bin/env python3
"""
Generates both Market Pulse pages:
  - index.html            (Daily Market Pulse: India + USA, 10 news-driven picks each)
  - tracker/index.html    (14-day forward tracker for the fixed 20-stock cohort)

Run by GitHub Actions on a cron schedule (see .github/workflows/daily-pulse.yml),
but you can also run it locally:

    export TAVILY_API_KEY=tvly-...
    export GEMINI_API_KEY=AIza...
    pip install -r requirements.txt
    python scripts/generate_pulse.py

ARCHITECTURE NOTE: search and writing are two separate steps, on purpose.
Gemini's own built-in Google Search grounding tool turned out to have a very
tight, separately-metered free-tier quota (it 429'd almost immediately even
though the underlying text-generation quota had plenty of headroom). So
instead: Tavily's search API (free, no credit card, 1,000 credits/month) does
the actual web research, and Gemini only ever sees plain text -- it just
reads the search results handed to it and writes the JSON. This uses
Gemini's much more generous plain-generation quota and avoids the grounding
tool entirely.

Get free keys at:
  - https://app.tavily.com          (Tavily -- no card required)
  - https://aistudio.google.com/apikey (Gemini -- no card required)

Claude (in the Cowork chat that produced this repo) could not test this
end-to-end against live API keys, so verify the first few runs by hand.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google import genai
from google.genai import types
from jinja2 import Environment, FileSystemLoader
from json_repair import repair_json

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
DATA_DIR = ROOT / "data"
COHORT_PATH = DATA_DIR / "cohort.json"
DAILY_OUT = ROOT / "index.html"
TRACKER_OUT = ROOT / "tracker" / "index.html"

MODEL = "gemini-flash-latest"  # Google-managed alias that always points at the
# current GA Flash model, so this doesn't break every time a dated version
# (like gemini-2.5-flash) gets retired for new API users.

TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
TAVILY_URL = "https://api.tavily.com/search"

gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def now_ist_string():
    """Best-effort IST timestamp for display. GitHub Actions runners are UTC,
    so we hand-offset by +5:30 rather than relying on system timezone data."""
    from datetime import timedelta

    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    date_line = ist_now.strftime("%a, %-d %b %Y") + " · " + ist_now.strftime("%-I:%M %p") + " IST (UTC+5:30)"
    compact = ist_now.strftime("%Y-%m-%d_%H%M") + "IST"
    return date_line, compact, ist_now


def extract_json(text: str) -> dict:
    """Pull the first {...} JSON object out of a text blob and parse it.
    The model is instructed to return ONLY JSON, but LLM output is often
    *almost* valid JSON -- a raw newline inside a string, a stray unescaped
    quote inside a company name or catalyst description, a trailing comma.
    Strategy: try strict parsing first (fast path); if that fails, fall back
    to json_repair, which is purpose-built to fix exactly this class of
    LLM-JSON error. Only raise if both fail."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model output:\n{text[:2000]}")
    raw = match.group(0)
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = repair_json(raw)
        return json.loads(repaired, strict=False)


def tavily_search(query: str, max_results: int = 5, search_depth: str = "basic", include_domains=None) -> str:
    """Query Tavily's search API and return a plain-text block (title + url +
    snippet per result) suitable for pasting into an LLM prompt as research
    context. Raises on HTTP error so a bad/missing key fails loudly rather
    than silently producing an empty briefing."""
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    resp = requests.post(TAVILY_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return "(no results found for this query)"
    lines = [f"- {r.get('title', '')} ({r.get('url', '')}): {r.get('content', '')}" for r in results]
    return "\n".join(lines)


def call_gemini(prompt: str, max_tokens: int = 8000) -> dict:
    """Plain (non-grounded) Gemini call -- the model only sees whatever
    search context we've already pasted into the prompt, no tools."""
    response = gemini_client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    full_text = response.text or ""
    return extract_json(full_text)


# ---------------------------------------------------------------------------
# STEP 1: Daily Market Pulse (fresh picks every run)
# ---------------------------------------------------------------------------

# Each entry is (query, kwargs-for-tavily_search). USA gets more/narrower
# queries and a nudge toward sites that publish explicit named-mover lists
# (marketwatch/cnbc/yahoo finance "Stock Market Today" style pieces),
# because generic USA queries were coming back with macro/geopolitical
# overview content instead of specific named stocks -- unlike India, where
# "top gainers/losers" listicle coverage is common enough that basic search
# finds it easily.
DAILY_SEARCH_QUERIES = [
    ("India stock market news today Sensex Nifty top gainers losers", {"max_results": 6}),
    ("India stocks in the news today order win earnings dividend buyback large mid small cap", {"max_results": 6}),
    ("US stock market today biggest gainers and losers specific stock names", {"max_results": 8, "search_depth": "advanced"}),
    ("US stocks in the news today earnings contract acquisition specific company mid cap small cap", {"max_results": 8, "search_depth": "advanced"}),
    (
        "stock market today biggest movers",
        {
            "max_results": 6,
            "search_depth": "advanced",
            "include_domains": ["marketwatch.com", "cnbc.com", "finance.yahoo.com", "investing.com"],
        },
    ),
]

DAILY_PROMPT_TEMPLATE = """You are building a "Daily Market Pulse" briefing, same format every day. Base your answer ONLY on the search context provided below -- it was fetched live moments ago, so treat it as current and do not fall back on older training-data knowledge about these companies.

SEARCH CONTEXT (from live web search just now):
{search_context}

Using only the information above (plus reasonable synthesis of it), return ONLY a valid JSON object (no prose before or after, no markdown code fence) with this exact shape. Every string value must be a single line -- no literal line breaks inside any string, use spaces instead:

{{
  "india": {{
    "index_cards": [
      {{"name": "Sensex", "val": "77,022.83", "chg": "+544.16 (+0.71%)", "cls": "up", "border_color": "#059669"}},
      {{"name": "Nifty 50", "val": "24,002.65", "chg": "+136.90 (+0.57%)", "cls": "up", "border_color": "#059669"}},
      {{"name": "Drivers", "val": "", "chg": "one or two sentence summary of what's driving the market today", "cls": "", "border_color": "#e8a317"}}
    ],
    "stocks": [
      {{"name": "Company Name", "ticker": "NSECODE", "cap": "Large", "price": "₹1,234.50", "price_note": "", "move": "+2.5%", "move_cls": "up", "why": "one or two sentences on the specific catalyst", "flag": ""}}
    ]
  }},
  "usa": {{
    "index_cards": [ ... same shape, for Dow/S&P/Nasdaq and a Drivers card ... ],
    "stocks": [ ... same shape as india.stocks ... ]
  }},
  "sources": ["domain1.com", "domain2.com", "..."],
  "footer_note": "one sentence noting this is a same-day news scan, not investment advice, built from web search"
}}

Rules:
- Exactly 10 stocks in india.stocks and 10 in usa.stocks, cap field one of "Large"/"Mid"/"Small", spread across all three as evenly as the actual search context supports (do not force false balance).
- USA section: if the context suggests US markets are closed (weekend/holiday), use the most recently completed session and say so in usa.index_cards drivers text.
- move_cls must be "up", "down", or "" (empty for non-directional items like "order win" or "buyback").
- "why" must cite a specific, concrete catalyst (earnings, order win, dividend, brokerage note, M&A, etc.) from the search context -- never vague filler like "sector is up".
- If a stock's price is stretched on valuation (very high P/E, huge run already priced in), set "flag" to a short risk note; otherwise leave flag as "".
- If a price is uncertain, missing, or conflicting in the search context, put the caveat in "price_note" (e.g. "quotes varied $145-$163") rather than presenting false precision.
- Never fabricate a stock, price, or catalyst that isn't actually supported by the search context above. If the context is too thin to fill all 10 slots for a market, use fewer stocks rather than inventing ones.
"""


def build_daily_pulse(env: Environment, date_line: str, compact_ts: str):
    context_blocks = []
    for q, kwargs in DAILY_SEARCH_QUERIES:
        context_blocks.append(f"### Search: {q}\n{tavily_search(q, **kwargs)}")
    search_context = "\n\n".join(context_blocks)

    prompt = DAILY_PROMPT_TEMPLATE.format(search_context=search_context)
    data = call_gemini(prompt)

    template = env.get_template("daily_pulse.html.j2")
    html = template.render(
        date_line=date_line,
        compact_timestamp=compact_ts,
        india=data["india"],
        usa=data["usa"],
        sources=data.get("sources", []),
        footer_note=data.get(
            "footer_note",
            "Same-day web news scan, not investment advice, not from a licensed research source.",
        ),
    )
    DAILY_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {DAILY_OUT}")


# ---------------------------------------------------------------------------
# STEP 2: Forward tracker (fixed cohort, re-priced each run)
# ---------------------------------------------------------------------------

REPRICE_PROMPT_TEMPLATE = """Based ONLY on the search context below (fetched live moments ago), determine the CURRENT share price for each of these {n} stocks. Return ONLY a valid JSON object (no prose, no markdown code fence, no literal line breaks inside any string value) mapping each ticker to its current price as a plain number (no currency symbols, no commas) plus an optional one-line note if the quote is stale/uncertain/conflicting in the context:

{{
  "TICKER1": {{"price": 1234.5, "note": ""}},
  "TICKER2": {{"price": 56.7, "note": "quotes ranged $54-$58"}}
}}

Stocks to look up (name / ticker / market):
{stock_list}

SEARCH CONTEXT (from live web search just now, one block per ticker):
{search_context}

Do not fabricate a price -- if the context truly doesn't contain a usable price for a ticker, omit that ticker from the JSON entirely rather than guessing.
"""


def fmt_price(value: float, currency: str) -> str:
    symbol = "₹" if currency == "INR" else "$"
    return f"{symbol}{value:,.2f}"


def build_tracker(env: Environment, date_line: str, compact_ts: str, ist_now: datetime):
    cohort_data = json.loads(COHORT_PATH.read_text(encoding="utf-8"))
    entry_date = datetime.strptime(cohort_data["entryDate"], "%Y-%m-%d")
    window_days = cohort_data["windowDays"]
    day_number = (ist_now.replace(tzinfo=None) - entry_date).days
    complete = day_number >= window_days
    day_label = f"Day {min(day_number, window_days)} of {window_days}"

    stock_list_lines = []
    price_context_blocks = []
    for s in cohort_data["cohort"]:
        stock_list_lines.append(f"- {s['name']} / {s['ticker']} / {s['market']}")
        query = f"{s['name']} {s['ticker']} share price today"
        price_context_blocks.append(f"### {s['ticker']}\n{tavily_search(query, max_results=3)}")

    prompt = REPRICE_PROMPT_TEMPLATE.format(
        n=len(cohort_data["cohort"]),
        stock_list="\n".join(stock_list_lines),
        search_context="\n\n".join(price_context_blocks),
    )
    prices = call_gemini(prompt, max_tokens=4000)

    india_rows, usa_rows = [], []
    changes = []
    cap_changes = {"Large": [], "Mid": [], "Small": []}

    for s in cohort_data["cohort"]:
        ticker = s["ticker"]
        current_price = s.get("lastPrice", s["entryPrice"])
        current_note = ""
        if ticker in prices:
            current_price = prices[ticker]["price"]
            current_note = prices[ticker].get("note", "")
            s["lastPrice"] = current_price  # persist for next run's fallback
        else:
            current_note = "no fresh quote found this run, carried forward"

        entry_price = s["entryPrice"]
        change_pct = (current_price - entry_price) / entry_price * 100
        changes.append(change_pct)
        cap_changes[s["cap"]].append(change_pct)
        change_cls = "up" if change_pct > 0 else ("down" if change_pct < 0 else "flat")

        row = {
            "name": s["name"],
            "ticker": ticker,
            "cap": s["cap"],
            "entry_price": fmt_price(entry_price, s["currency"]),
            "entry_note": s.get("note", ""),
            "current_price": fmt_price(current_price, s["currency"]),
            "current_note": current_note,
            "change_pct": f"{change_pct:+.1f}%",
            "change_cls": change_cls,
            "why": s["why"],
        }
        (india_rows if s["market"] == "India" else usa_rows).append(row)

    up = sum(1 for c in changes if c > 0)
    down = sum(1 for c in changes if c < 0)
    flat = sum(1 for c in changes if c == 0)
    avg = sum(changes) / len(changes) if changes else 0.0

    footer_bits = [
        f"Day {min(day_number, window_days)} read: {up} up, {down} down, {flat} flat, average {avg:+.1f}%."
    ]
    for cap in ("Large", "Mid", "Small"):
        vals = cap_changes[cap]
        if vals:
            footer_bits.append(f"{cap} cap avg {sum(vals)/len(vals):+.1f}%.")
    if complete:
        footer_bits.append("The 14-day window is complete -- treat this as a finished experiment, not a live signal.")
    footer_note = " ".join(footer_bits)

    template = env.get_template("tracker.html.j2")
    html = template.render(
        day_label=day_label,
        checked_line=date_line,
        complete=complete,
        stats={"up": up, "down": down, "flat": flat, "avg": f"{avg:+.1f}%"},
        india_rows=india_rows,
        usa_rows=usa_rows,
        footer_note=footer_note,
        compact_timestamp=compact_ts,
    )
    TRACKER_OUT.parent.mkdir(parents=True, exist_ok=True)
    TRACKER_OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {TRACKER_OUT}")

    cohort_data["lastChecked"] = ist_now.strftime("%Y-%m-%d")
    COHORT_PATH.write_text(json.dumps(cohort_data, indent=2), encoding="utf-8")
    print(f"Updated {COHORT_PATH}")


def main():
    date_line, compact_ts, ist_now = now_ist_string()
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))

    try:
        build_daily_pulse(env, date_line, compact_ts)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR building daily pulse: {exc}", file=sys.stderr)
        raise

    try:
        build_tracker(env, date_line, compact_ts, ist_now)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR building tracker: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
