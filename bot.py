import asyncio
import os
import json
import httpx
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_KEY")
SERPER_API_KEY     = os.getenv("SERPER_API_KEY",     "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "openrouter/free")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_VOLUME            = int(os.getenv("MIN_VOLUME",    "1000"))
MIN_EDGE              = float(os.getenv("MIN_EDGE",    "0.08"))
MAX_MARKETS_PER_SCAN  = int(os.getenv("MAX_MARKETS",  "50"))
BATCH_SIZE            = int(os.getenv("BATCH_SIZE",   "10"))
DB_PATH               = os.getenv("DB_PATH",          "polybot.db")
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
seen_market_ids: set = set()

# Master lookup: market_id -> full market data (prices, question, slug, url)
# This ensures we NEVER show AI-hallucinated data in alerts
market_registry: dict = {}


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                joined_at  TEXT,
                active     INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                market_id   TEXT,
                question    TEXT,
                direction   TEXT,
                entry_price REAL,
                amount      REAL,
                opened_at   TEXT,
                closed_at   TEXT,
                exit_price  REAL,
                pnl         REAL,
                status      TEXT DEFAULT 'open'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at   TEXT,
                markets_seen INTEGER,
                alerts_sent  INTEGER,
                top_edge     REAL
            )
        """)
        conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def add_user(chat_id, username, first_name):
    with get_db() as conn:
        conn.execute("""INSERT OR IGNORE INTO users (chat_id, username, first_name, joined_at)
                        VALUES (?,?,?,?)""",
                     (chat_id, username, first_name, datetime.now(timezone.utc).isoformat()))
        conn.execute("UPDATE users SET active=1 WHERE chat_id=?", (chat_id,))

def remove_user(chat_id):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=0 WHERE chat_id=?", (chat_id,))

def get_active_users():
    with get_db() as conn:
        return [r["chat_id"] for r in conn.execute("SELECT chat_id FROM users WHERE active=1").fetchall()]

def get_user_count():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]

def log_scan(markets_seen, alerts_sent, top_edge):
    with get_db() as conn:
        conn.execute("INSERT INTO scan_log (scanned_at,markets_seen,alerts_sent,top_edge) VALUES (?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), markets_seen, alerts_sent, top_edge))

def get_scan_stats():
    with get_db() as conn:
        row = conn.execute("""SELECT COUNT(*) as total_scans, SUM(alerts_sent) as total_alerts,
                                     MAX(top_edge) as best_edge FROM scan_log""").fetchone()
    return dict(row) if row else {}

def log_trade(chat_id, market_id, question, direction, entry_price, amount):
    with get_db() as conn:
        conn.execute("""INSERT INTO trades (chat_id,market_id,question,direction,entry_price,amount,opened_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (chat_id, market_id, question, direction, entry_price, amount,
                      datetime.now(timezone.utc).isoformat()))

def close_trade(trade_id, exit_price):
    with get_db() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not t: return None
        pnl = (exit_price - t["entry_price"]) * t["amount"] if t["direction"] == "YES" \
              else (t["entry_price"] - exit_price) * t["amount"]
        conn.execute("UPDATE trades SET status='closed',exit_price=?,pnl=?,closed_at=? WHERE id=?",
                     (exit_price, pnl, datetime.now(timezone.utc).isoformat(), trade_id))
        return pnl

def get_portfolio(chat_id):
    with get_db() as conn:
        opens  = conn.execute("SELECT * FROM trades WHERE chat_id=? AND status='open' ORDER BY opened_at DESC", (chat_id,)).fetchall()
        stats  = conn.execute("""SELECT COUNT(*) as total,
                                        SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                                        SUM(pnl) as total_pnl
                                 FROM trades WHERE chat_id=? AND status='closed'""", (chat_id,)).fetchone()
    return {"open": [dict(t) for t in opens], "stats": dict(stats) if stats else {}}


# ── Polymarket API ────────────────────────────────────────────────────────────

def parse_prices(market: dict) -> tuple:
    """Return (yes_price, no_price) from a Gamma market object."""
    outcomes = market.get("outcomes", "[]")
    prices   = market.get("outcomePrices", "[]")
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except: outcomes = []
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except: prices = []

    price_map = {}
    for o, p in zip(outcomes, prices):
        try: price_map[str(o).upper()] = round(float(p), 4)
        except: pass

    yes = price_map.get("YES", 0.0)
    no  = price_map.get("NO",  round(1 - yes, 4) if yes else 0.0)
    return yes, no

async def fetch_markets() -> list:
    """Fetch top active events+markets from Polymarket Gamma API."""
    # Use /events endpoint — it returns the event slug used in Polymarket URLs
    url = "https://gamma-api.polymarket.com/events"
    params = {
        "active": "true", "closed": "false",
        "limit": MAX_MARKETS_PER_SCAN, "offset": 0,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url, params=params)
        resp.raise_for_status()
        raw = resp.json()

    events = raw if isinstance(raw, list) else raw.get("data", [])

    # Flatten: each event contains one or more markets
    all_markets = []
    for event in events:
        event_slug   = event.get("slug", "")
        event_volume = float(event.get("volume", 0) or 0)
        event_url    = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

        for m in event.get("markets", []):
            m["_event_slug"] = event_slug
            m["_event_url"]  = event_url
            m["_event_vol"]  = event_volume
            all_markets.append(m)

    filtered = [m for m in all_markets if float(m.get("volume", 0) or 0) >= MIN_VOLUME]
    filtered.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)

    # Register each market with REAL data including the correct event URL
    for m in filtered:
        mid = str(m.get("id", ""))
        yes_price, no_price = parse_prices(m)
        market_registry[mid] = {
            "id":        mid,
            "question":  m.get("question", ""),
            "slug":      m.get("_event_slug", m.get("slug", "")),
            "url":       m.get("_event_url", ""),
            "yes_price": yes_price,
            "no_price":  no_price,
            "volume":    float(m.get("volume", 0) or 0),
            "end_date":  m.get("endDate", ""),
        }

    sample = list(market_registry.values())[0] if market_registry else {}
    print(f"  Sample: {sample.get('question','')[:50]} | URL: {sample.get('url','')}")
    return filtered


# ── News Search ───────────────────────────────────────────────────────────────

async def search_news(query: str) -> str:
    if not SERPER_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                "https://google.serper.dev/search",
                headers={"X-API-Key": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 3, "tbs": "qdr:w"},
            )
            resp.raise_for_status()
            snippets = [r.get("snippet", "") for r in resp.json().get("organic", [])[:3]]
            return " | ".join(s for s in snippets if s)
    except Exception as e:
        print(f"  Search error: {e}")
        return ""

async def enrich_with_news(markets: list) -> list:
    if not SERPER_API_KEY:
        return markets
    print(f"  Fetching news for {len(markets)} markets...")
    for m in markets:
        q = " ".join(m.get("question", "").split()[:7])
        m["_news"] = await search_news(q)
        await asyncio.sleep(0.3)
    return markets


# ── AI Analysis ───────────────────────────────────────────────────────────────

# The AI only receives: market_id, question, yes_price, no_price, volume, end_date, news
# It returns ONLY: market_id, direction, true_prob_estimate, edge_pct, confidence, reasoning
# We then look up ALL display data from market_registry — never trust AI for prices/questions

SYSTEM_PROMPT = """You are a Polymarket prediction market analyst.

You are given real market data with EXACT current prices.
Your ONLY job is to judge whether the YES price shown is significantly WRONG.

CRITICAL RULES:
1. Use ONLY the market_id, question, YES/NO prices exactly as shown — do NOT invent or change them
2. Return market_id EXACTLY as given in the input
3. Only flag markets where you believe the price is wrong by at least 8 percentage points
4. Base your judgment on: the news provided, your knowledge, and logical reasoning
5. Be conservative — 2-3 real picks beats 10 guesses

For each opportunity return ONLY these fields:
- market_id: copy exactly from input
- direction: "YES" or "NO"  
- true_prob_estimate: your estimate as a decimal (e.g. 0.75)
- edge_pct: abs(your_estimate - market_price) * 100
- confidence: "HIGH" (edge>20%), "MEDIUM" (10-20%), "LOW" (8-10%)
- reasoning: 2 sentences max, reference specific news or logic
- news_used: true or false

DO NOT include question, market_price, or slug — we have those already.

Respond ONLY in valid JSON, no markdown:
{
  "opportunities": [
    {
      "market_id": "...",
      "direction": "YES",
      "true_prob_estimate": 0.00,
      "edge_pct": 0.0,
      "confidence": "HIGH",
      "reasoning": "...",
      "news_used": false
    }
  ],
  "scan_summary": "One sentence summary."
}"""

FALLBACK_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
]

def format_batch_for_ai(markets: list) -> str:
    """Build a clean, unambiguous market list for the AI."""
    lines = []
    for m in markets:
        mid       = str(m.get("id", "?"))
        question  = m.get("question", "N/A")
        end_date  = m.get("endDate", "N/A")
        volume    = float(m.get("volume", 0) or 0)
        yes_price, no_price = parse_prices(m)
        news      = m.get("_news", "")

        line = (
            f"MARKET_ID: {mid}\n"
            f"QUESTION: {question}\n"
            f"YES_PRICE: {yes_price:.4f} ({yes_price:.1%})\n"
            f"NO_PRICE:  {no_price:.4f} ({no_price:.1%})\n"
            f"VOLUME: ${volume:,.0f} | ENDS: {end_date}"
        )
        if news:
            line += f"\nRECENT_NEWS: {news[:250]}"
        lines.append(line)

    return "\n---\n".join(lines)

async def analyze_batch(batch_text: str, batch_num: int) -> list:
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
                "model": model, "max_tokens": 2000,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Analyze batch {batch_num}:\n\n{batch_text}"},
                ],
            }
            async with httpx.AsyncClient(timeout=90) as http:
                resp = await http.post("https://openrouter.ai/api/v1/chat/completions",
                                       headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not content:
                print(f"    Batch {batch_num}: {model} empty, trying next...")
                continue

            raw    = content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            opps   = result.get("opportunities", [])
            print(f"    Batch {batch_num}: {model} → {len(opps)} opportunities")
            return opps

        except Exception as e:
            print(f"    Batch {batch_num}: {model} failed ({type(e).__name__}: {e})")
            await asyncio.sleep(2)

    return []

async def analyze_all_markets(markets: list) -> list:
    batches  = [markets[i:i+BATCH_SIZE] for i in range(0, len(markets), BATCH_SIZE)]
    all_opps = []

    print(f"  Analyzing {len(markets)} markets in {len(batches)} batches...")
    for i, batch in enumerate(batches, 1):
        opps = await analyze_batch(format_batch_for_ai(batch), i)
        all_opps.extend(opps)
        if i < len(batches):
            await asyncio.sleep(4)

    # Deduplicate and sort by edge
    seen, unique = set(), []
    for o in sorted(all_opps, key=lambda x: float(x.get("edge_pct", 0)), reverse=True):
        mid = str(o.get("market_id", ""))
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(o)

    return unique


# ── Alert Formatting — uses market_registry for ALL real data ─────────────────

def format_opportunity(opp: dict) -> str:
    mid        = str(opp.get("market_id", ""))
    direction  = opp.get("direction", "?")
    true_prob  = float(opp.get("true_prob_estimate", 0))
    edge_pct   = float(opp.get("edge_pct", 0))
    confidence = opp.get("confidence", "?")
    reasoning  = opp.get("reasoning", "")
    news_used  = opp.get("news_used", False)

    # ── Pull REAL data from registry, never from AI ──
    reg = market_registry.get(mid, {})
    question    = reg.get("question", "Unknown market")
    url         = reg.get("url", "")
    volume      = reg.get("volume", 0)
    end_date    = reg.get("end_date", "")
    market_price = reg.get("yes_price", 0) if direction == "YES" else reg.get("no_price", 0)

    # Sanity check — skip if prices are missing
    if market_price == 0 and mid not in market_registry:
        return ""

    conf_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "🌀"}.get(confidence, "❓")
    dir_emoji  = "✅" if direction == "YES" else "❌"
    profit_pct = round(((true_prob - market_price) / market_price) * 100, 1) if market_price > 0 else 0
    edge_bar   = "█" * min(int(edge_pct / 5), 10) + "░" * max(0, 10 - int(edge_pct / 5))
    news_tag   = " 📰" if news_used else ""
    end_str    = f" | Ends: {end_date[:10]}" if end_date else ""

    msg = (
        f"{conf_emoji} <b>{confidence} CONFIDENCE</b>{news_tag}\n"
        f"{dir_emoji} <b>BET {direction}</b>\n"
        f"📋 {question}\n\n"
        f"💰 Current Price : <b>{market_price:.0%}</b>\n"
        f"🎯 AI Estimate   : <b>{true_prob:.0%}</b>\n"
        f"📊 Edge [{edge_bar}] <b>{edge_pct:.1f}%</b>\n"
        f"💵 Potential     : +{profit_pct:.1f}%\n"
        f"📦 Volume: ${volume:,.0f}{end_str}\n\n"
        f"💡 {reasoning}\n"
    )
    if url:
        msg += (
            f"\n🔗 <a href='{url}'>Open on Polymarket</a>\n"
            f"📝 <code>/addtrade {mid} {direction} {market_price:.4f} &lt;amount&gt;</code>"
        )
    return msg.strip()


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_message(chat_id: int, text: str):
    if not text:
        return
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            await http.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            })
        except Exception as e:
            print(f"  Send error {chat_id}: {e}")

async def broadcast(text: str):
    for chat_id in get_active_users():
        await send_message(chat_id, text)
        await asyncio.sleep(0.1)

async def get_updates(offset=0):
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(f"{TELEGRAM_API}/getUpdates",
                              params={"offset": offset, "timeout": 20, "limit": 100})
        resp.raise_for_status()
        return resp.json().get("result", [])


# ── Command Handlers ──────────────────────────────────────────────────────────

async def handle_start(chat_id, username, first_name):
    add_user(chat_id, username, first_name)
    count = get_user_count()
    await send_message(chat_id,
        f"🤖 <b>Polymarket Sniper Bot</b>\n\n"
        f"Hey {first_name}! Subscribed to live AI trade alerts.\n\n"
        f"🔍 Scans {MAX_MARKETS_PER_SCAN} markets every {SCAN_INTERVAL_MINUTES} min\n"
        f"📊 Real-time prices from Polymarket API\n"
        f"🧠 AI judges edge, never invents data\n"
        + (f"📰 News fact-checking enabled\n" if SERPER_API_KEY else "") +
        f"\n<b>Commands:</b> /portfolio /addtrade /closetrade /stats /help\n\n"
        f"👥 {count} traders subscribed\n"
        f"⚠️ <i>Information only. Always DYOR.</i>"
    )

async def handle_stop(chat_id):
    remove_user(chat_id)
    await send_message(chat_id, "👋 Unsubscribed. Send /start anytime to resubscribe.")

async def handle_portfolio(chat_id):
    data  = get_portfolio(chat_id)
    opens = data["open"]
    stats = data["stats"]
    if not opens and not stats.get("total"):
        await send_message(chat_id, "📂 No trades yet.\nUse /addtrade after an alert to start tracking.")
        return
    msg = "📂 <b>Portfolio</b>\n\n"
    if opens:
        msg += f"<b>🟢 Open ({len(opens)})</b>\n"
        for t in opens[:5]:
            msg += f"#{t['id']} {t['direction']} @ {t['entry_price']:.2%} — ${t['amount']:.0f}\n"
            msg += f"   <i>{str(t['question'])[:45]}...</i>\n"
        msg += "\n"
    if stats.get("total"):
        total, wins = stats["total"] or 0, stats["wins"] or 0
        pnl         = stats["total_pnl"] or 0
        wr          = round(wins/total*100) if total else 0
        msg += (f"<b>📊 Record:</b> {wins}W/{total-wins}L ({wr}% win rate)\n"
                f"{'📈' if pnl>=0 else '📉'} PnL: <b>${pnl:+.2f}</b>")
    await send_message(chat_id, msg)

async def handle_addtrade(chat_id, args):
    if len(args) < 4:
        await send_message(chat_id,
            "📝 <b>Log a Trade</b>\n\n"
            "Usage: <code>/addtrade &lt;market_id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;usd_amount&gt;</code>\n"
            "Example: <code>/addtrade 558970 NO 0.7200 100</code>")
        return
    try:
        mid, direction = args[0], args[1].upper()
        price, amount  = float(args[2]), float(args[3])
        question = market_registry.get(mid, {}).get("question", "Manual trade")
        if direction not in ("YES","NO"): raise ValueError("Direction must be YES or NO")
        log_trade(chat_id, mid, question, direction, price, amount)
        with get_db() as conn:
            tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        await send_message(chat_id,
            f"✅ <b>Trade #{tid} Logged</b>\n"
            f"{direction} @ {price:.2%} — ${amount:.0f}\n"
            f"<i>{question[:50]}</i>\n\n"
            f"Close: <code>/closetrade {tid} &lt;exit_price&gt;</code>")
    except Exception as e:
        await send_message(chat_id, f"❌ {e}")

async def handle_closetrade(chat_id, args):
    if len(args) < 2:
        await send_message(chat_id, "Usage: <code>/closetrade &lt;id&gt; &lt;exit_price&gt;</code>")
        return
    try:
        pnl = close_trade(int(args[0]), float(args[1]))
        if pnl is None:
            await send_message(chat_id, "❌ Trade not found.")
            return
        await send_message(chat_id,
            f"{'🟢' if pnl>=0 else '🔴'} <b>Trade #{args[0]} Closed</b>\n"
            f"Exit: {float(args[1]):.0%} | PnL: <b>${pnl:+.2f}</b>")
    except Exception as e:
        await send_message(chat_id, f"❌ {e}")

async def handle_users(chat_id):
    stats = get_scan_stats()
    await send_message(chat_id,
        f"👥 <b>{get_user_count()}</b> active subscribers\n\n"
        f"📡 Total scans : {stats.get('total_scans',0)}\n"
        f"🔔 Total alerts: {int(stats.get('total_alerts',0) or 0)}\n"
        f"🏆 Best edge   : {stats.get('best_edge',0) or 0:.1f}%")

async def handle_help(chat_id):
    await send_message(chat_id,
        "<b>Commands</b>\n\n"
        "/start — Subscribe\n"
        "/stop — Unsubscribe\n"
        "/portfolio — Open trades + stats\n"
        "/stats — Win rate & PnL\n"
        "/addtrade &lt;id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;amount&gt;\n"
        "/closetrade &lt;id&gt; &lt;exit_price&gt;\n"
        "/users — Subscriber count\n"
        "/help — This menu")


# ── Scan Loop ─────────────────────────────────────────────────────────────────

async def run_scan():
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting scan...")
    if get_user_count() == 0:
        print("  No subscribers, skipping.")
        return
    try:
        markets = await fetch_markets()
        print(f"  Fetched {len(markets)} markets")
        if not markets:
            return

        markets  = await enrich_with_news(markets)
        all_opps = await analyze_all_markets(markets)
        strong   = [o for o in all_opps if float(o.get("edge_pct",0)) >= MIN_EDGE * 100]
        top_edge = float(strong[0].get("edge_pct",0)) if strong else 0.0

        print(f"  {len(strong)} strong opportunities (top edge: {top_edge:.1f}%)")

        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if not strong:
            await broadcast(
                f"🔍 <b>Scan — {now}</b>\n"
                f"Scanned {len(markets)} markets. No strong edges this round."
            )
        else:
            await broadcast(
                f"🚀 <b>Polymarket Scan — {now}</b>\n"
                f"Scanned <b>{len(markets)}</b> markets • "
                f"Found <b>{len(strong)}</b> opportunit{'y' if len(strong)==1 else 'ies'}\n"
                f"Top edge: <b>{top_edge:.1f}%</b>"
                + (" 📰" if SERPER_API_KEY else "")
            )
            sent = 0
            for opp in strong:
                mid = str(opp.get("market_id",""))
                if mid in seen_market_ids:
                    continue
                msg = format_opportunity(opp)
                if msg:
                    await broadcast(msg)
                    seen_market_ids.add(mid)
                    sent += 1
                    await asyncio.sleep(1)

        log_scan(len(markets), len(strong), top_edge)
        print(f"  Broadcast complete ✓")
    except Exception as e:
        print(f"  Scan error: {e}")
        import traceback; traceback.print_exc()


# ── Telegram Polling ──────────────────────────────────────────────────────────

async def poll_updates():
    offset = 0
    print("  Polling for Telegram messages...")
    while True:
        try:
            updates = await get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                if not msg: continue
                chat_id    = msg["chat"]["id"]
                username   = msg.get("from",{}).get("username","")
                first_name = msg.get("from",{}).get("first_name","User")
                text       = msg.get("text","").strip()
                if not text.startswith("/"): continue
                parts   = text.split()
                command = parts[0].split("@")[0].lower()
                args    = parts[1:]
                if   command == "/start":                  await handle_start(chat_id, username, first_name)
                elif command == "/stop":                   await handle_stop(chat_id)
                elif command in ("/portfolio","/stats"):   await handle_portfolio(chat_id)
                elif command == "/addtrade":               await handle_addtrade(chat_id, args)
                elif command == "/closetrade":             await handle_closetrade(chat_id, args)
                elif command == "/users":                  await handle_users(chat_id)
                elif command == "/help":                   await handle_help(chat_id)
        except Exception as e:
            print(f"  Poll error: {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────

async def scan_loop():
    while True:
        await run_scan()
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)

async def main():
    init_db()
    print("=" * 55)
    print("  Polymarket Sniper Bot")
    print(f"  Markets/scan : {MAX_MARKETS_PER_SCAN} (batches of {BATCH_SIZE})")
    print(f"  Model        : {OPENROUTER_MODEL}")
    print(f"  News search  : {'✅ on' if SERPER_API_KEY else '❌ off'}")
    print(f"  Min edge     : {MIN_EDGE*100:.0f}%")
    print(f"  Subscribers  : {get_user_count()}")
    print("=" * 55)
    await asyncio.gather(poll_updates(), scan_loop())

if __name__ == "__main__":
    asyncio.run(main())
