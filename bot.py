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
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "openrouter/free")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL", "30"))
MIN_VOLUME            = int(os.getenv("MIN_VOLUME",    "1000"))
MIN_EDGE              = float(os.getenv("MIN_EDGE",    "0.10"))
MAX_MARKETS_PER_SCAN  = int(os.getenv("MAX_MARKETS",  "10"))
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
                active      INTEGER DEFAULT 1
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

def add_user(chat_id: int, username: str, first_name: str):
    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (chat_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
        """, (chat_id, username, first_name, datetime.now(timezone.utc).isoformat()))
        conn.execute("UPDATE users SET active=1 WHERE chat_id=?", (chat_id,))

def remove_user(chat_id: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=0 WHERE chat_id=?", (chat_id,))

def get_active_users() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM users WHERE active=1").fetchall()
    return [r["chat_id"] for r in rows]

def get_user_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]

def log_trade(chat_id: int, market_id: str, question: str, direction: str,
              entry_price: float, amount: float):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO trades (chat_id, market_id, question, direction, entry_price, amount, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, market_id, question, direction, entry_price, amount,
              datetime.now(timezone.utc).isoformat()))

def close_trade(trade_id: int, exit_price: float):
    with get_db() as conn:
        trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not trade:
            return None
        # PnL: positive if direction=YES and price went up, or NO and price went down
        if trade["direction"] == "YES":
            pnl = (exit_price - trade["entry_price"]) * trade["amount"]
        else:
            pnl = (trade["entry_price"] - exit_price) * trade["amount"]
        conn.execute("""
            UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
            WHERE id=?
        """, (exit_price, pnl, datetime.now(timezone.utc).isoformat(), trade_id))
        return pnl

def get_portfolio(chat_id: int) -> dict:
    with get_db() as conn:
        open_trades = conn.execute(
            "SELECT * FROM trades WHERE chat_id=? AND status='open' ORDER BY opened_at DESC",
            (chat_id,)
        ).fetchall()
        closed_trades = conn.execute(
            "SELECT * FROM trades WHERE chat_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 10",
            (chat_id,)
        ).fetchall()
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl
            FROM trades WHERE chat_id=? AND status='closed'
        """, (chat_id,)).fetchone()
    return {
        "open": [dict(t) for t in open_trades],
        "closed": [dict(t) for t in closed_trades],
        "stats": dict(stats) if stats else {}
    }


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            resp = await http.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            resp.raise_for_status()
        except Exception as e:
            print(f"  Failed to send to {chat_id}: {e}")

async def broadcast(text: str):
    users = get_active_users()
    print(f"  Broadcasting to {len(users)} users...")
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


# ── Command handlers ──────────────────────────────────────────────────────────

async def handle_start(chat_id: int, username: str, first_name: str):
    add_user(chat_id, username, first_name)
    count = get_user_count()
    await send_message(chat_id,
        f"🤖 <b>Welcome to Polymarket Sniper Bot!</b>\n\n"
        f"Hey {first_name}! You're now subscribed to AI-powered trade alerts.\n\n"
        f"📊 I scan Polymarket every {SCAN_INTERVAL_MINUTES} minutes and alert you when I find mispriced markets.\n\n"
        f"<b>Commands:</b>\n"
        f"/start — Subscribe to alerts\n"
        f"/stop — Unsubscribe\n"
        f"/portfolio — View your tracked trades\n"
        f"/addtrade — Log a trade you took\n"
        f"/closetrade — Close a trade with exit price\n"
        f"/stats — Your win rate & PnL\n"
        f"/users — Total subscribers\n\n"
        f"👥 {count} traders already subscribed!\n\n"
        f"⚠️ <i>This bot provides information only. Always do your own research.</i>"
    )

async def handle_stop(chat_id: int):
    remove_user(chat_id)
    await send_message(chat_id,
        "👋 You've been unsubscribed from alerts.\n"
        "Send /start anytime to resubscribe."
    )

async def handle_portfolio(chat_id: int):
    data = get_portfolio(chat_id)
    open_trades = data["open"]
    stats = data["stats"]

    if not open_trades and not stats.get("total"):
        await send_message(chat_id,
            "📂 <b>Your Portfolio</b>\n\n"
            "No trades logged yet.\n"
            "Use /addtrade to log a trade you took from an alert."
        )
        return

    msg = "📂 <b>Your Portfolio</b>\n\n"

    if open_trades:
        msg += f"<b>🟢 Open Trades ({len(open_trades)})</b>\n"
        for t in open_trades[:5]:
            msg += (
                f"#{t['id']} {t['direction']} — {t['question'][:40]}...\n"
                f"   Entry: {t['entry_price']:.2f} | Amount: ${t['amount']:.0f}\n"
            )
        msg += "\n"

    if stats.get("total"):
        total   = stats["total"] or 0
        wins    = stats["wins"] or 0
        losses  = stats["losses"] or 0
        pnl     = stats["total_pnl"] or 0
        win_rate = round((wins / total) * 100) if total > 0 else 0
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        msg += (
            f"<b>📊 Closed Trade Stats</b>\n"
            f"Total: {total} | Wins: {wins} | Losses: {losses}\n"
            f"Win Rate: {win_rate}%\n"
            f"{pnl_emoji} Total PnL: ${pnl:+.2f}\n"
        )

    await send_message(chat_id, msg)

async def handle_stats(chat_id: int):
    await handle_portfolio(chat_id)

async def handle_addtrade(chat_id: int, args: list):
    # Usage: /addtrade <market_id> <YES|NO> <entry_price> <amount>
    # e.g.  /addtrade abc123 YES 0.45 50
    if len(args) < 4:
        await send_message(chat_id,
            "📝 <b>Log a Trade</b>\n\n"
            "Usage: <code>/addtrade &lt;market_id&gt; &lt;YES|NO&gt; &lt;entry_price&gt; &lt;amount_usd&gt;</code>\n\n"
            "Example: <code>/addtrade abc123 NO 0.72 100</code>\n\n"
            "Find the market ID in the alert link or on Polymarket."
        )
        return
    try:
        market_id   = args[0]
        direction   = args[1].upper()
        entry_price = float(args[2])
        amount      = float(args[3])
        question    = " ".join(args[4:]) if len(args) > 4 else "Manual trade"

        if direction not in ("YES", "NO"):
            raise ValueError("Direction must be YES or NO")
        if not 0 < entry_price < 1:
            raise ValueError("Entry price must be between 0 and 1")

        log_trade(chat_id, market_id, question, direction, entry_price, amount)

        with get_db() as conn:
            trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        await send_message(chat_id,
            f"✅ <b>Trade Logged!</b>\n\n"
            f"ID: #{trade_id}\n"
            f"Direction: {direction}\n"
            f"Entry: {entry_price:.2f} ({entry_price:.0%})\n"
            f"Amount: ${amount:.0f}\n\n"
            f"Use <code>/closetrade {trade_id} &lt;exit_price&gt;</code> to close it."
        )
    except Exception as e:
        await send_message(chat_id, f"❌ Error: {e}\n\nUsage: /addtrade &lt;market_id&gt; &lt;YES|NO&gt; &lt;entry_price&gt; &lt;amount&gt;")

async def handle_closetrade(chat_id: int, args: list):
    if len(args) < 2:
        await send_message(chat_id,
            "📝 <b>Close a Trade</b>\n\n"
            "Usage: <code>/closetrade &lt;trade_id&gt; &lt;exit_price&gt;</code>\n\n"
            "Example: <code>/closetrade 3 0.91</code>"
        )
        return
    try:
        trade_id   = int(args[0])
        exit_price = float(args[1])
        pnl = close_trade(trade_id, exit_price)
        if pnl is None:
            await send_message(chat_id, "❌ Trade not found.")
            return
        emoji = "🟢" if pnl >= 0 else "🔴"
        await send_message(chat_id,
            f"{emoji} <b>Trade Closed!</b>\n\n"
            f"Trade #{trade_id}\n"
            f"Exit price: {exit_price:.2f} ({exit_price:.0%})\n"
            f"PnL: <b>${pnl:+.2f}</b>"
        )
    except Exception as e:
        await send_message(chat_id, f"❌ Error: {e}")

async def handle_users(chat_id: int):
    count = get_user_count()
    await send_message(chat_id, f"👥 <b>{count}</b> active subscribers")

async def handle_help(chat_id: int):
    await send_message(chat_id,
        "<b>📋 Commands</b>\n\n"
        "/start — Subscribe to alerts\n"
        "/stop — Unsubscribe\n"
        "/portfolio — Open trades & stats\n"
        "/addtrade &lt;id&gt; &lt;YES|NO&gt; &lt;price&gt; &lt;amount&gt; — Log a trade\n"
        "/closetrade &lt;id&gt; &lt;exit_price&gt; — Close a trade\n"
        "/stats — Win rate & PnL summary\n"
        "/users — Total subscribers\n"
        "/help — Show this menu"
    )


# ── Polymarket API ────────────────────────────────────────────────────────────

async def fetch_markets() -> list:
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

def format_markets_for_ai(markets: list) -> str:
    lines = []
    for m in markets:
        question  = m.get("question", "N/A")
        end_date  = m.get("endDate", "N/A")
        volume    = float(m.get("volume", 0) or 0)
        market_id = m.get("id", "?")

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

        lines.append(
            f"ID: {market_id} | Q: {question} | "
            f"YES: {yes_price:.2f} | NO: {no_price:.2f} | "
            f"Vol: ${volume:,.0f} | Ends: {end_date}"
        )
    return "\n".join(lines)


# ── AI Analysis ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Polymarket prediction market analyst.
Identify mispriced or high-edge trading opportunities.

For each opportunity provide:
- Whether to bet YES or NO
- Your estimated true probability vs the market price
- Edge percentage
- Confidence: LOW / MEDIUM / HIGH
- Brief reasoning (1-2 sentences)

Be conservative — 2-3 strong picks beats 10 weak ones.

Respond ONLY with valid JSON, no markdown fences:
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
  "scan_summary": "One sentence summary."
}"""

FALLBACK_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
]

async def analyze_markets_with_ai(markets_text: str) -> dict:
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
                    {"role": "user", "content": "Analyze these markets:\n\n" + markets_text},
                ],
            }
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not content:
                print(f"  {model} returned empty, trying next...")
                continue

            raw = content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"  Used model: {model}")
            return result

        except Exception as e:
            print(f"  {model} failed: {e}, trying next...")
            await asyncio.sleep(2)

    return {"opportunities": [], "scan_summary": "AI unavailable, retrying next scan."}

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
        msg += (
            f"🔗 <a href='{link}'>Open on Polymarket</a>\n\n"
            f"📝 To track: <code>/addtrade {market_id} {direction} {market_price:.2f} &lt;amount&gt;</code>"
        )
    return msg


# ── Scan & Broadcast ──────────────────────────────────────────────────────────

async def run_scan():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting scan...")
    user_count = get_user_count()
    if user_count == 0:
        print("  No subscribers yet, skipping broadcast.")
        return

    try:
        markets = await fetch_markets()
        print(f"  Fetched {len(markets)} markets")
        if not markets:
            return

        result  = await analyze_markets_with_ai(format_markets_for_ai(markets))
        opps    = result.get("opportunities", [])
        summary = result.get("scan_summary", "")
        strong  = [o for o in opps if float(o.get("edge_pct", 0)) >= MIN_EDGE * 100]

        print(f"  Found {len(strong)} strong opportunities, broadcasting to {user_count} users")

        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        if not strong:
            await broadcast(
                f"🔍 <b>Scan — {now}</b>\n"
                f"Scanned {len(markets)} markets. No strong edges found.\n"
                f"📊 {summary}"
            )
        else:
            await broadcast(
                f"🚀 <b>Scan — {now}</b>\n"
                f"Scanned {len(markets)} markets • "
                f"Found <b>{len(strong)}</b> opportunit{'y' if len(strong)==1 else 'ies'}\n"
                f"📊 {summary}"
            )
            for opp in strong:
                mid = opp.get("market_id", "")
                if mid and mid in seen_market_ids:
                    continue
                await broadcast(format_opportunity(opp))
                if mid:
                    seen_market_ids.add(mid)
                await asyncio.sleep(1)

        print("  Broadcast complete ✓")
    except Exception as e:
        print(f"  Scan error: {e}")


# ── Update Polling Loop ───────────────────────────────────────────────────────

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

                parts   = text.split()
                command = parts[0].split("@")[0].lower()
                args    = parts[1:]

                if command == "/start":
                    await handle_start(chat_id, username, first_name)
                elif command == "/stop":
                    await handle_stop(chat_id)
                elif command == "/portfolio":
                    await handle_portfolio(chat_id)
                elif command == "/stats":
                    await handle_stats(chat_id)
                elif command == "/addtrade":
                    await handle_addtrade(chat_id, args)
                elif command == "/closetrade":
                    await handle_closetrade(chat_id, args)
                elif command == "/users":
                    await handle_users(chat_id)
                elif command == "/help":
                    await handle_help(chat_id)

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
    print("=" * 50)
    print("  Polymarket Sniper Bot — Multi-User")
    print(f"  Model         : {OPENROUTER_MODEL}")
    print(f"  Scan interval : {SCAN_INTERVAL_MINUTES} min")
    print(f"  Subscribers   : {get_user_count()}")
    print("=" * 50)
    # Run polling and scanning concurrently
    await asyncio.gather(poll_updates(), scan_loop())

if __name__ == "__main__":
    asyncio.run(main())
