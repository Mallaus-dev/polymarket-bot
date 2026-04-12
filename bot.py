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
SERPER_API_KEY     = os.getenv("SERPER_API_KEY",     "")   # optional: serper.dev for web search
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "openrouter/free")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_VOLUME            = int(os.getenv("MIN_VOLUME",    "1000"))
MIN_EDGE              = float(os.getenv("MIN_EDGE",    "0.08"))
MAX_MARKETS_PER_SCAN  = int(os.getenv("MAX_MARKETS",  "50"))
BATCH_SIZE            = int(os.getenv("BATCH_SIZE",   "10"))  # markets per AI call
DB_PATH               = os.getenv("DB_PATH",          "polybot.db")
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
seen_market_ids: set = set()


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                joined_at   TEXT,
                active      INTEGER DEFAULT 1,
                tier        TEXT DEFAULT 'free'
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
                status      TEXT DEFAULT 'open',
                FOREIGN KEY(chat_id) REFERENCES users(chat_id)
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
        conn.execute("""
            INSERT OR IGNORE INTO users (chat_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
        """, (chat_id, username, first_name, datetime.now(timezone.utc).isoformat()))
        conn.execute("UPDATE users SET active=1 WHERE chat_id=?", (chat_id,))

def remove_user(chat_id):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=0 WHERE chat_id=?", (chat_id,))

def get_active_users():
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM users WHERE active=1").fetchall()
    return [r["chat_id"] for r in rows]

def get_user_count():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]

def log_scan(markets_seen, alerts_sent, top_edge):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_log (scanned_at, markets_seen, alerts_sent, top_edge) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), markets_seen, alerts_sent, top_edge)
        )

def get_scan_stats():
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as total_scans,
                   SUM(alerts_sent) as total_alerts,
                   AVG(markets_seen) as avg_markets,
                   MAX(top_edge) as best_edge
            FROM scan_log
        """).fetchone()
    return dict(row) if row else {}

def log_trade(chat_id, market_id, question, direction, entry_price, amount):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO trades (chat_id, market_id, question, direction, entry_price, amount, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, market_id, question, direction, entry_price, amount,
              datetime.now(timezone.utc).isoformat()))

def close_trade(trade_id, exit_price):
    with get_db() as conn:
        trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not trade:
            return None
        pnl = (exit_price - trade["entry_price"]) * trade["amount"] if trade["direction"] == "YES" \
              else (trade["entry_price"] - exit_price) * trade["amount"]
        conn.execute("""
            UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=? WHERE id=?
        """, (exit_price, pnl, datetime.now(timezone.utc).isoformat(), trade_id))
        return pnl

def get_portfolio(chat_id):
    with get_db() as conn:
        open_trades   = conn.execute(
            "SELECT * FROM trades WHERE chat_id=? AND status='open' ORDER BY opened_at DESC",
            (chat_id,)).fetchall()
        closed_trades = conn.execute(
            "SELECT * FROM trades WHERE chat_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 10",
            (chat_id,)).fetchall()
        stats = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                   SUM(pnl) as total_pnl
            FROM trades WHERE chat_id=? AND status='closed'
        """, (chat_id,)).fetchone()
    return {
        "open":   [dict(t) for t in open_trades],
        "closed": [dict(t) for t in closed_trades],
        "stats":  dict(stats) if stats else {},
    }


# ── Web Search (Serper) ───────────────────────────────────────────────────────

async def search_news(query: str) -> str:
    """Search for recent news about a market topic. Returns top 3 snippets."""
    if not SERPER_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                "https://google.serper.dev/search",
                headers={"X-API-Key": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": 3, "tbs": "qdr:w"},  # past week
            )
            resp.raise_for_status()
            data = resp.json()
        snippets = [r.get("snippet", "") for r in data.get("organic", [])[:3]]
        return " | ".join(snippets)
    except Exception as e:
        print(f"  Search error: {e}")
        return ""

async def enrich_markets_with_news(markets: list) -> list:
    """Add recent news context to each market."""
    if not SERPER_API_KEY:
        return markets
    print(f"  Searching news for {len(markets)} markets...")
    for m in markets:
        question = m.get("question", "")
        # Extract key terms — first 8 words
        short_q = " ".join(question.split()[:8])
        news = await search_news(short_q)
        m["_news"] = news
        await asyncio.sleep(0.3)  # rate limit
    return markets


# ── Polymarket API ────────────────────────────────────────────────────────────

async def fetch_markets() -> list:
    """Fetch top markets from Polymarket Gamma API."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true", "closed": "false",
        "limit": MAX_MARKETS_PER_SCAN, "offset": 0,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(url, params=params)
        resp.raise_for_status()
        raw = resp.json()

    markets = raw if isinstance(raw, list) else raw.get("data", raw.get("markets", []))
    filtered = [m for m in markets if float(m.get("volume", 0) or 0) >= MIN_VOLUME]
    filtered.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
    return filtered

def format_batch_for_ai(markets: list) -> str:
    lines = []
    for m in markets:
        question  = m.get("question", "N/A")
        end_date  = m.get("endDate", "N/A")
        volume    = float(m.get("volume", 0) or 0)
        market_id = m.get("id", "?")
        slug      = m.get("slug", "")
        news      = m.get("_news", "")

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
        no_price  = price_map.get("NO", 1 - yes_price if yes_price else 0)

        line = (
            f"ID: {market_id} | SLUG: {slug} | Q: {question} | "
            f"YES: {yes_price:.2f} | NO: {no_price:.2f} | "
            f"Vol: ${volume:,.0f} | Ends: {end_date}"
        )
        if news:
            line += f"\n  RECENT NEWS: {news[:300]}"
        lines.append(line)
    return "\n\n".join(lines)


# ── AI Analysis ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Polymarket prediction market analyst.
You are given a list of active markets, their current prices, volumes, and recent news.

Your job: identify markets where the current price is SIGNIFICANTLY wrong based on:
- Recent news provided
- Your knowledge of base rates and similar events
- Logic about how likely the outcome truly is

For EACH opportunity return:
- market_id and slug (copy exactly from input)
- direction: YES or NO
- market_price: the current price shown
- true_prob_estimate: your estimated real probability (0.0 to 1.0)
- edge_pct: abs(true_prob - market_price) * 100
- confidence: HIGH (>20% edge), MEDIUM (10-20%), LOW (<10%)
- reasoning: 2-3 sentences referencing specific news or logic
- news_used: true/false — did recent news influence your call?

Be selective. Only return markets with genuine edge. Quality over quantity.

Respond ONLY in valid JSON, no markdown:
{
  "opportunities": [
    {
      "market_id": "...",
      "slug": "...",
      "question": "...",
      "direction": "YES",
      "market_price": 0.00,
      "true_prob_estimate": 0.00,
      "edge_pct": 0.0,
      "confidence": "HIGH",
      "reasoning": "...",
      "news_used": true
    }
  ],
  "scan_summary": "One sentence summary of what you found."
}"""

FALLBACK_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

async def analyze_batch(batch_text: str, batch_num: int) -> dict:
    """Analyze one batch of markets with the AI."""
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
                resp = await http.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not content:
                print(f"    Batch {batch_num}: {model} empty, trying next...")
                continue

            raw = content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"    Batch {batch_num}: used {model}, found {len(result.get('opportunities', []))} opps")
            return result

        except Exception as e:
            print(f"    Batch {batch_num}: {model} failed ({e}), trying next...")
            await asyncio.sleep(2)

    return {"opportunities": [], "scan_summary": ""}

async def analyze_all_markets(markets: list) -> list:
    """Split markets into batches, analyze each, return all opportunities sorted by edge."""
    batches = [markets[i:i+BATCH_SIZE] for i in range(0, len(markets), BATCH_SIZE)]
    print(f"  Analyzing {len(markets)} markets in {len(batches)} batches of {BATCH_SIZE}...")

    all_opps = []
    for i, batch in enumerate(batches, 1):
        batch_text = format_batch_for_ai(batch)
        result = await analyze_batch(batch_text, i)
        all_opps.extend(result.get("opportunities", []))
        if i < len(batches):
            await asyncio.sleep(3)  # avoid rate limiting between batches

    # Deduplicate by market_id and sort by edge descending
    seen = set()
    unique = []
    for o in sorted(all_opps, key=lambda x: float(x.get("edge_pct", 0)), reverse=True):
        mid = o.get("market_id", "")
        if mid not in seen:
            seen.add(mid)
            unique.append(o)

    return unique


# ── Alert Formatting ──────────────────────────────────────────────────────────

def format_opportunity(opp: dict) -> str:
    direction    = opp.get("direction", "?")
    question     = opp.get("question", "?")
    market_price = float(opp.get("market_price", 0))
    true_prob    = float(opp.get("true_prob_estimate", 0))
    edge_pct     = float(opp.get("edge_pct", 0))
    confidence   = opp.get("confidence", "?")
    reasoning    = opp.get("reasoning", "")
    market_id    = opp.get("market_id", "")
    slug         = opp.get("slug", "")
    news_used    = opp.get("news_used", False)

    conf_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "🌀"}.get(confidence, "❓")
    dir_emoji  = "✅" if direction == "YES" else "❌"
    profit_pct = round(((true_prob - market_price) / market_price) * 100, 1) if market_price > 0 else 0
    news_tag   = " 📰" if news_used else ""

    link_id = slug if slug else market_id
    link    = f"https://polymarket.com/event/{link_id}" if link_id else ""

    # Edge bar (visual)
    edge_bar = "█" * min(int(edge_pct / 5), 10) + "░" * max(0, 10 - int(edge_pct / 5))

    msg = (
        f"{conf_emoji} <b>{confidence} CONFIDENCE</b>{news_tag}\n"
        f"{dir_emoji} <b>BET {direction}</b>\n"
        f"📋 {question}\n\n"
        f"💰 Market Price : {market_price:.0%}\n"
        f"🎯 True Estimate: {true_prob:.0%}\n"
        f"📊 Edge  [{edge_bar}] {edge_pct:.1f}%\n"
        f"💵 Potential    : +{profit_pct:.1f}%\n\n"
        f"💡 {reasoning}\n"
    )
    if link:
        msg += (
            f"\n🔗 <a href='{link}'>Open on Polymarket</a>\n"
            f"📝 <code>/addtrade {market_id} {direction} {market_price:.2f} &lt;amount&gt;</code>"
        )
    return msg


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_message(chat_id: int, text: str):
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            resp = await http.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            resp.raise_for_status()
        except Exception as e:
            print(f"  Send error to {chat_id}: {e}")

async def broadcast(text: str):
    users = get_active_users()
    for chat_id in users:
        await send_message(chat_id, text)
        await asyncio.sleep(0.1)

async def get_updates(offset: int = 0) -> list:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(f"{TELEGRAM_API}/getUpdates", params={
            "offset": offset, "timeout": 20, "limit": 100
        })
        resp.raise_for_status()
        return resp.json().get("result", [])


# ── Command Handlers ──────────────────────────────────────────────────────────

async def handle_start(chat_id, username, first_name):
    add_user(chat_id, username, first_name)
    count = get_user_count()
    await send_message(chat_id,
        f"🤖 <b>Polymarket Sniper Bot</b>\n\n"
        f"Hey {first_name}! You're subscribed to AI-powered trade alerts.\n\n"
        f"🔍 Scans {MAX_MARKETS_PER_SCAN} markets every {SCAN_INTERVAL_MINUTES} minutes\n"
        f"🧠 AI fact-checks with real-time news\n"
        f"📊 Track your trades and win rate\n\n"
        f"<b>Commands:</b>\n"
        f"/portfolio — Open trades & stats\n"
        f"/addtrade — Log a trade\n"
        f"/closetrade — Close with exit price\n"
        f"/stats — Win rate & PnL\n"
        f"/stop — Unsubscribe\n"
        f"/help — Full command list\n\n"
        f"👥 {count} traders subscribed\n\n"
        f"⚠️ <i>Information only. Always DYOR.</i>"
    )

async def handle_stop(chat_id):
    remove_user(chat_id)
    await send_message(chat_id, "👋 Unsubscribed. Send /start to resubscribe anytime.")

async def handle_portfolio(chat_id):
    data  = get_portfolio(chat_id)
    open_trades = data["open"]
    stats = data["stats"]

    if not open_trades and not stats.get("total"):
        await send_message(chat_id,
            "📂 <b>Portfolio</b>\n\nNo trades yet.\n"
            "Use /addtrade after receiving an alert to start tracking.")
        return

    msg = "📂 <b>Your Portfolio</b>\n\n"
    if open_trades:
        msg += f"<b>🟢 Open ({len(open_trades)})</b>\n"
        for t in open_trades[:5]:
            msg += f"#{t['id']} {t['direction']} @ {t['entry_price']:.2f} — ${t['amount']:.0f}\n"
            msg += f"   <i>{t['question'][:45]}...</i>\n"
        msg += "\n"

    if stats.get("total"):
        total    = stats["total"] or 0
        wins     = stats["wins"] or 0
        pnl      = stats["total_pnl"] or 0
        win_rate = round((wins / total) * 100) if total > 0 else 0
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        msg += (
            f"<b>📊 Closed Trades</b>\n"
            f"Record: {wins}W / {(total-wins)}L ({win_rate}% win rate)\n"
            f"{pnl_emoji} Total PnL: <b>${pnl:+.2f}</b>"
        )

    await send_message(chat_id, msg)

async def handle_addtrade(chat_id, args):
    if len(args) < 4:
        await send_message(chat_id,
            "📝 <b>Log a Trade</b>\n\n"
            "Usage: <code>/addtrade &lt;market_id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;amount_usd&gt;</code>\n"
            "Example: <code>/addtrade 12345 NO 0.72 100</code>"
        )
        return
    try:
        market_id, direction = args[0], args[1].upper()
        entry_price, amount  = float(args[2]), float(args[3])
        question = " ".join(args[4:]) if len(args) > 4 else "From alert"

        if direction not in ("YES", "NO"): raise ValueError("Direction must be YES or NO")
        if not 0 < entry_price < 1: raise ValueError("Price must be 0-1 (e.g. 0.45)")

        log_trade(chat_id, market_id, question, direction, entry_price, amount)
        with get_db() as conn:
            trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        await send_message(chat_id,
            f"✅ <b>Trade #{trade_id} Logged</b>\n\n"
            f"{direction} @ {entry_price:.2f} ({entry_price:.0%}) — ${amount:.0f}\n\n"
            f"Close it later: <code>/closetrade {trade_id} &lt;exit_price&gt;</code>"
        )
    except Exception as e:
        await send_message(chat_id, f"❌ {e}\n\nUsage: /addtrade &lt;id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;amount&gt;")

async def handle_closetrade(chat_id, args):
    if len(args) < 2:
        await send_message(chat_id,
            "Usage: <code>/closetrade &lt;trade_id&gt; &lt;exit_price&gt;</code>\n"
            "Example: <code>/closetrade 3 0.91</code>"
        )
        return
    try:
        pnl = close_trade(int(args[0]), float(args[1]))
        if pnl is None:
            await send_message(chat_id, "❌ Trade not found.")
            return
        emoji = "🟢" if pnl >= 0 else "🔴"
        await send_message(chat_id,
            f"{emoji} <b>Trade #{args[0]} Closed</b>\n\n"
            f"Exit: {float(args[1]):.0%}\nPnL: <b>${pnl:+.2f}</b>"
        )
    except Exception as e:
        await send_message(chat_id, f"❌ {e}")

async def handle_users(chat_id):
    count = get_user_count()
    stats = get_scan_stats()
    msg = (
        f"👥 <b>{count}</b> active subscribers\n\n"
        f"📡 Total scans: {stats.get('total_scans', 0)}\n"
        f"🔔 Total alerts sent: {int(stats.get('total_alerts', 0) or 0)}\n"
        f"🏆 Best edge found: {stats.get('best_edge', 0):.1f}%"
    )
    await send_message(chat_id, msg)

async def handle_help(chat_id):
    await send_message(chat_id,
        "<b>📋 All Commands</b>\n\n"
        "/start — Subscribe\n"
        "/stop — Unsubscribe\n"
        "/portfolio — Open trades + stats\n"
        "/stats — Win rate & PnL\n"
        "/addtrade &lt;id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;amount&gt;\n"
        "/closetrade &lt;id&gt; &lt;exit_price&gt;\n"
        "/users — Subscriber count & bot stats\n"
        "/help — This menu"
    )


# ── Scan & Broadcast ──────────────────────────────────────────────────────────

async def run_scan():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan...")
    user_count = get_user_count()
    if user_count == 0:
        print("  No subscribers yet.")
        return

    try:
        # 1. Fetch markets
        markets = await fetch_markets()
        print(f"  Fetched {len(markets)} markets")
        if not markets:
            return

        # 2. Enrich with news if Serper key is available
        markets = await enrich_markets_with_news(markets)

        # 3. Analyze in batches
        all_opps = await analyze_all_markets(markets)
        strong   = [o for o in all_opps if float(o.get("edge_pct", 0)) >= MIN_EDGE * 100]
        top_edge = float(strong[0].get("edge_pct", 0)) if strong else 0.0

        print(f"  {len(all_opps)} total opportunities, {len(strong)} above {MIN_EDGE*100:.0f}% edge threshold")

        # 4. Broadcast
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if not strong:
            await broadcast(
                f"🔍 <b>Scan — {now}</b>\n"
                f"Scanned {len(markets)} markets across {len(markets)//BATCH_SIZE + 1} batches.\n"
                f"No strong edges found this round."
            )
        else:
            await broadcast(
                f"🚀 <b>Polymarket Scan — {now}</b>\n"
                f"Scanned <b>{len(markets)}</b> markets • "
                f"Found <b>{len(strong)}</b> opportunit{'y' if len(strong)==1 else 'ies'}\n"
                f"Top edge: <b>{top_edge:.1f}%</b>"
                + (" 📰 news-verified" if SERPER_API_KEY else "")
            )
            alerts_sent = 0
            for opp in strong:
                mid = opp.get("market_id", "")
                if mid and mid in seen_market_ids:
                    continue
                await broadcast(format_opportunity(opp))
                if mid:
                    seen_market_ids.add(mid)
                alerts_sent += 1
                await asyncio.sleep(1)

        log_scan(len(markets), len(strong), top_edge)
        print(f"  Broadcast complete ✓")

    except Exception as e:
        print(f"  Scan error: {e}")


# ── Update Polling ────────────────────────────────────────────────────────────

async def poll_updates():
    offset = 0
    print("  Polling for Telegram messages...")
    while True:
        try:
            updates = await get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg:
                    continue

                chat_id    = msg["chat"]["id"]
                username   = msg.get("from", {}).get("username", "")
                first_name = msg.get("from", {}).get("first_name", "User")
                text       = msg.get("text", "").strip()

                if not text.startswith("/"):
                    continue

                parts, command, args = text.split(), "", []
                command = parts[0].split("@")[0].lower()
                args    = parts[1:]

                if command == "/start":         await handle_start(chat_id, username, first_name)
                elif command == "/stop":        await handle_stop(chat_id)
                elif command in ("/portfolio", "/stats"): await handle_portfolio(chat_id)
                elif command == "/addtrade":    await handle_addtrade(chat_id, args)
                elif command == "/closetrade":  await handle_closetrade(chat_id, args)
                elif command == "/users":       await handle_users(chat_id)
                elif command == "/help":        await handle_help(chat_id)

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
    news_status = "✅ enabled" if SERPER_API_KEY else "❌ disabled (add SERPER_API_KEY)"
    print("=" * 55)
    print("  Polymarket Sniper Bot — Phase 1 Upgrade")
    print(f"  Markets per scan : {MAX_MARKETS_PER_SCAN} (in batches of {BATCH_SIZE})")
    print(f"  Model            : {OPENROUTER_MODEL}")
    print(f"  News search      : {news_status}")
    print(f"  Scan interval    : {SCAN_INTERVAL_MINUTES} min")
    print(f"  Min edge         : {MIN_EDGE*100:.0f}%")
    print(f"  Subscribers      : {get_user_count()}")
    print("=" * 55)
    await asyncio.gather(poll_updates(), scan_loop())

if __name__ == "__main__":
    asyncio.run(main())
