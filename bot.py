import asyncio
import os
import json
import httpx
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_KEY")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "meta-llama/llama-3.3-70b-instruct:free")

SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_VOLUME            = int(os.getenv("MIN_VOLUME",    "1000"))
MIN_EDGE              = float(os.getenv("MIN_EDGE",    "0.10"))
MAX_MARKETS_PER_SCAN  = int(os.getenv("MAX_MARKETS",  "60"))
# ─────────────────────────────────────────────────────────────────────────────

seen_market_ids: set = set()


# ── Polymarket Gamma API ──────────────────────────────────────────────────────

async def fetch_markets() -> list:
    """Fetch active markets from Polymarket Gamma API."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": MAX_MARKETS_PER_SCAN,
        "offset": 0,
        "order": "volume24hr",
        "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url, params=params)
        resp.raise_for_status()
        raw = resp.json()

    # Gamma returns a plain list
    markets = raw if isinstance(raw, list) else raw.get("data", raw.get("markets", []))

    filtered = []
    for m in markets:
        vol = float(m.get("volume", 0) or m.get("volumeNum", 0) or 0)
        if vol >= MIN_VOLUME:
            filtered.append(m)

    filtered.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
    return filtered


def format_markets_for_ai(markets: list) -> str:
    lines = []
    for m in markets:
        question  = m.get("question", "N/A")
        end_date  = m.get("endDate", m.get("end_date_iso", "N/A"))
        volume    = float(m.get("volume", 0) or 0)
        market_id = m.get("id", m.get("conditionId", "?"))

        # Gamma API stores outcomes and prices as JSON strings or lists
        outcomes = m.get("outcomes", "[]")
        prices   = m.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []

        price_map = {}
        for o, p in zip(outcomes, prices):
            try: price_map[str(o).upper()] = float(p)
            except: pass

        yes_price = price_map.get("YES", 0)
        no_price  = price_map.get("NO",  1 - yes_price if yes_price else 0)

        lines.append(
            f"ID: {market_id} | Q: {question} | "
            f"YES: {yes_price:.2f} | NO: {no_price:.2f} | "
            f"Vol: ${volume:,.0f} | Ends: {end_date}"
        )
    return "\n".join(lines)


# ── AI Analysis via OpenRouter ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Polymarket prediction market analyst.
Your job is to identify mispriced or high-edge trading opportunities.

For each opportunity, provide:
- Whether to bet YES or NO
- Your estimated true probability vs the market price
- Edge percentage
- Confidence level: LOW / MEDIUM / HIGH
- Brief reasoning (1-2 sentences)

Only flag markets where the price appears significantly wrong. Be conservative —
2-3 strong picks beats 10 weak ones.

Respond ONLY with valid JSON, no markdown fences, no extra text:
{
  "opportunities": [
    {
      "market_id": "...",
      "question": "...",
      "direction": "YES or NO",
      "market_price": 0.00,
      "true_prob_estimate": 0.00,
      "edge_pct": 0.0,
      "confidence": "HIGH or MEDIUM or LOW",
      "reasoning": "..."
    }
  ],
  "scan_summary": "One sentence summary of market conditions."
}"""


# Fallback models if primary fails
FALLBACK_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

async def analyze_markets_with_ai(markets_text: str) -> dict:
    """Send markets to OpenRouter with fallback model support."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/polymarket-sniper",
        "X-Title": "Polymarket Sniper Bot",
    }

    models_to_try = [OPENROUTER_MODEL] + [m for m in FALLBACK_MODELS if m != OPENROUTER_MODEL]

    for model in models_to_try:
        try:
            payload = {
                "model": model,
                "max_tokens": 2000,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "Analyze these markets and find the best opportunities:\n\n" + markets_text},
                ],
            }
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not content:
                print(f"  Model {model} returned empty response, trying next...")
                continue

            raw = content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"  Used model: {model}")
            return result

        except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError) as e:
            print(f"  Model {model} failed: {e}, trying next...")
            await asyncio.sleep(2)
            continue

    # All models failed — return empty result
    return {"opportunities": [], "scan_summary": "All AI models unavailable, try again later."}


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        resp.raise_for_status()


def format_opportunity(opp: dict) -> str:
    direction    = opp.get("direction", "?")
    question     = opp.get("question", "?")
    market_price = float(opp.get("market_price", 0))
    true_prob    = float(opp.get("true_prob_estimate", 0))
    edge_pct     = float(opp.get("edge_pct", 0))
    confidence   = opp.get("confidence", "?")
    reasoning    = opp.get("reasoning", "")
    market_id    = opp.get("market_id", "")

    conf_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "🌀"}.get(confidence, "❓")
    dir_emoji  = "✅" if direction == "YES" else "❌"
    profit_pct = round(((true_prob - market_price) / market_price) * 100, 1) if market_price > 0 else 0
    link       = f"https://polymarket.com/event/{market_id}" if market_id else ""

    msg = (
        f"{conf_emoji} <b>{confidence} CONFIDENCE</b>\n"
        f"{dir_emoji} <b>BET {direction}</b>\n"
        f"📋 {question}\n"
        f"💰 Market: {market_price:.0%}  |  Edge: {edge_pct:.1f}%\n"
        f"🎯 True Prob: {true_prob:.0%}  |  Potential: +{profit_pct:.1f}%\n"
        f"💡 {reasoning}\n"
    )
    if link:
        msg += f"🔗 <a href='{link}'>Open on Polymarket</a>\n"
    return msg


async def send_scan_results(opportunities: list, summary: str, market_count: int):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if not opportunities:
        await send_telegram(
            f"🔍 <b>Polymarket Scan — {now}</b>\n"
            f"Scanned {market_count} markets. No strong edges found.\n"
            f"📊 {summary}"
        )
        return

    await send_telegram(
        f"🚀 <b>Polymarket Scan — {now}</b>\n"
        f"Scanned {market_count} markets • "
        f"Found <b>{len(opportunities)}</b> opportunit{'y' if len(opportunities) == 1 else 'ies'}\n"
        f"📊 {summary}"
    )
    for opp in opportunities:
        mid = opp.get("market_id", "")
        if mid and mid in seen_market_ids:
            continue
        await send_telegram(format_opportunity(opp))
        if mid:
            seen_market_ids.add(mid)
        await asyncio.sleep(0.5)


# ── Main Loop ─────────────────────────────────────────────────────────────────

async def run_scan():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan...")
    try:
        markets = await fetch_markets()
        print(f"  Fetched {len(markets)} markets above ${MIN_VOLUME:,} volume")

        if not markets:
            print("  No markets found. Skipping.")
            return

        markets_text = format_markets_for_ai(markets)
        result       = await analyze_markets_with_ai(markets_text)

        opps    = result.get("opportunities", [])
        summary = result.get("scan_summary", "")
        strong  = [o for o in opps if float(o.get("edge_pct", 0)) >= MIN_EDGE * 100]

        print(f"  AI found {len(opps)} opportunities, {len(strong)} above edge threshold")
        await send_scan_results(strong, summary, len(markets))
        print("  Telegram alerts sent ✓")

    except Exception as e:
        err = f"⚠️ Bot error: {e}"
        print(err)
        try:
            await send_telegram(err)
        except Exception:
            pass


async def main():
    print("=" * 50)
    print("  Polymarket Sniper Bot — Starting Up")
    print(f"  Model         : {OPENROUTER_MODEL}")
    print(f"  Scan interval : {SCAN_INTERVAL_MINUTES} min")
    print(f"  Min volume    : ${MIN_VOLUME:,}")
    print(f"  Min edge      : {MIN_EDGE * 100:.0f}%")
    print("=" * 50)

    await send_telegram(
        "🤖 <b>Polymarket Sniper Bot Online</b>\n"
        f"Model: <code>{OPENROUTER_MODEL}</code>\n"
        f"Scanning every {SCAN_INTERVAL_MINUTES} minutes.\n"
        f"Min volume: ${MIN_VOLUME:,} | Min edge: {MIN_EDGE * 100:.0f}%"
    )

    while True:
        await run_scan()
        print(f"  Sleeping {SCAN_INTERVAL_MINUTES} min...\n")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
