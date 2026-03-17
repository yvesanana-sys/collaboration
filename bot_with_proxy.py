import os
import time
import json
import httpx
import requests
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify
from flask_cors import CORS

ALPACA_KEY    = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
GROK_KEY      = os.environ.get("GROK_KEY", "")
BASE_URL      = "https://api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets"
BOT_NAME      = os.environ.get("BOT_NAME", "collaboration")

RULES = {
    "budget":               55,
    "daily_loss_limit_pct": 0.05,
    "max_positions":        5,
    "stop_loss_pct":        0.04,
    "take_profit_pct":      0.07,
    "min_confidence":       80,
    "interval_minutes":     5,
    "autonomy_threshold":   150,   # Each AI gets autonomy budget when equity > this
    "universe": [
        "NVDA","AMD","TSLA","META","AMZN",
        "PLTR","SOFI","MSTR","COIN","RKLB",
        "AAPL","MSFT","GOOGL","NFLX","CRM",
    ],
}

# ── Shared state between AIs ─────────────────────────────
shared_state = {
    "claude_positions": [],    # Positions Claude owns
    "grok_positions":   [],    # Positions Grok owns
    "claude_budget":    0,     # Claude's allocated budget
    "grok_budget":      0,     # Grok's allocated budget
    "autonomy_mode":    False, # True when each AI trades independently
    "last_sync":        None,
}

app = Flask(__name__)
CORS(app)

def alpaca_get(path):
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    res = requests.get(BASE_URL + path, headers=headers)
    res.raise_for_status()
    return res.json()

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME, "mode": "REAL",
                    "autonomy": shared_state["autonomy_mode"]})

@app.route("/stats")
def stats():
    try:
        account   = alpaca_get("/v2/account")
        positions = alpaca_get("/v2/positions")
        equity    = float(account["equity"])
        return jsonify({
            "bot":           BOT_NAME,
            "equity":        equity,
            "cash":          float(account["cash"]),
            "pnl":           round(equity - RULES["budget"], 2),
            "pnl_pct":       round((equity - RULES["budget"]) / RULES["budget"] * 100, 2),
            "mode":          "REAL",
            "autonomy_mode": shared_state["autonomy_mode"],
            "claude_owns":   shared_state["claude_positions"],
            "grok_owns":     shared_state["grok_positions"],
            "positions": [
                {"symbol": p["symbol"], "qty": p["qty"],
                 "pnl": round(float(p["unrealized_pl"]), 2),
                 "pnl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
                 "owner": "claude" if p["symbol"] in shared_state["claude_positions"]
                          else "grok" if p["symbol"] in shared_state["grok_positions"]
                          else "shared"}
                for p in positions
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def alpaca(method, path, body=None, base=None):
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }
    res = requests.request(method, (base or BASE_URL) + path, headers=headers, json=body)
    res.raise_for_status()
    return res.json()

def get_bars(symbol, days=60):
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=days+10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&feed=iex&adjustment=raw"
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            url2 = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&adjustment=raw"
            res = requests.get(url2, headers=headers, timeout=10)
        if res.ok:
            bars = res.json().get("bars", [])
            if bars: return bars
        return []
    except Exception as e:
        log(f"⚠️ Bars failed {symbol}: {e}")
        return []

def compute_indicators(bars):
    if len(bars) < 26: return None
    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    def sma(data, n): return sum(data[-n:]) / n if len(data) >= n else None
    def ema(data, n):
        k = 2/(n+1); e = data[0]
        for p in data[1:]: e = p*k + e*(1-k)
        return e
    def rsi(data, n=14):
        g,l = [],[]
        for i in range(1,len(data)):
            d = data[i]-data[i-1]; g.append(max(d,0)); l.append(max(-d,0))
        if len(g)<n: return None
        ag=sum(g[-n:])/n; al=sum(l[-n:])/n
        return round(100-(100/(1+ag/al)),2) if al else 100
    close=closes[-1]; sma20=sma(closes,20); sma50=sma(closes,50)
    ema9=ema(closes[-20:],9); ema21=ema(closes[-30:],21)
    macd=round(ema(closes[-30:],12)-ema(closes,26),4)
    rsi_v=rsi(closes)
    bb_mid=sma20
    if bb_mid:
        std=(sum((c-bb_mid)**2 for c in closes[-20:])/20)**0.5
        bb_u=bb_mid+2*std; bb_l=bb_mid-2*std
        bb_pct=round((close-bb_l)/(bb_u-bb_l)*100,1) if bb_u!=bb_l else 50
    else: bb_pct=None
    avg_vol=sum(volumes[-20:])/20 if len(volumes)>=20 else None
    vol_ratio=round(volumes[-1]/avg_vol,2) if avg_vol else None
    # Price change % last 5 days
    momentum_5d = round((closes[-1]-closes[-6])/closes[-6]*100,2) if len(closes)>=6 else None
    return {
        "close":round(close,2),"rsi":rsi_v,"macd":macd,
        "sma20":round(sma20,2) if sma20 else None,
        "sma50":round(sma50,2) if sma50 else None,
        "ema9":round(ema9,2),"ema21":round(ema21,2),
        "bb_pct":bb_pct,"vol_ratio":vol_ratio,
        "momentum_5d":momentum_5d,
    }

def get_news_context():
    """Fetch recent market news headlines via Alpaca"""
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        symbols = ",".join(RULES["universe"][:8])
        url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbols}&start={start}&end={end}&limit=20&sort=desc"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            articles = res.json().get("news", [])
            headlines = []
            for a in articles[:15]:
                sym = a.get("symbols", ["?"])[0] if a.get("symbols") else "MARKET"
                headlines.append(f"  [{sym}] {a.get('headline','')}")
            if headlines:
                return "\n".join(headlines)
        return "  No recent news available"
    except Exception as e:
        return f"  News fetch failed: {e}"

def get_market_context():
    """Get broader market snapshot"""
    try:
        # Get SPY and QQQ as market proxies
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        symbols = "SPY,QQQ,VIX"
        url = f"{DATA_URL}/v2/stocks/snapshots?symbols={symbols}"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            data = res.json()
            lines = []
            for sym, snap in data.items():
                bar = snap.get("dailyBar", {})
                prev = snap.get("prevDailyBar", {})
                if bar and prev:
                    chg = round((bar.get("c",0) - prev.get("c",0)) / prev.get("c",1) * 100, 2)
                    lines.append(f"  {sym}: ${bar.get('c',0):.2f} ({'+' if chg>=0 else ''}{chg}%)")
            return "\n".join(lines) if lines else "  Market data unavailable"
        return "  Market context unavailable"
    except Exception as e:
        return f"  Market context error: {e}"

def get_chart_section():
    lines = []
    for sym in RULES["universe"]:
        bars=get_bars(sym); ind=compute_indicators(bars)
        if not ind: lines.append(f"  {sym}: insufficient data"); continue
        lines.append(
            f"  {sym}: ${ind['close']} RSI={ind['rsi']} MACD={ind['macd']} "
            f"SMA20={ind['sma20']} SMA50={ind['sma50']} EMA9={ind['ema9']} EMA21={ind['ema21']} "
            f"BB%={ind['bb_pct']} Vol={ind['vol_ratio']} Mom5d={ind['momentum_5d']}%"
        )
    return "\n".join(lines)

def estimate_fees(notional):
    """Alpaca charges $0 commission but has SEC/FINRA fees"""
    sec_fee   = max(notional * 0.0000278, 0.01)
    finra_fee = min(notional * 0.000145, 7.27)
    return round(sec_fee + finra_fee, 4)

def ask_claude(prompt, system="You are a trading AI. Respond with ONLY valid JSON. No markdown."):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["content"][0]["text"]

def ask_grok(prompt, system="You are a trading AI. Respond with ONLY valid JSON. No markdown."):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-3-mini", "max_tokens": 2000,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["choices"][0]["message"]["content"]

def parse_json(raw):
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find("{"); e = raw.rfind("}") + 1
    if s == -1 or e == 0: return None
    return json.loads(raw[s:e])

def parse_json_list(raw):
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find("["); e = raw.rfind("]") + 1
    if s == -1 or e == 0:
        obj = parse_json(raw)
        return [obj] if obj else []
    return json.loads(raw[s:e])

def is_market_open():
    return alpaca("GET", "/v2/clock").get("is_open", False)

def get_market_mode():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins   = now_et.hour * 60 + now_et.minute
    if mins < 510 or mins >= 1020:   return "sleep",      60
    elif 510 <= mins < 570:          return "premarket",  15
    elif 570 <= mins < 630:          return "opening",     5
    elif 630 <= mins < 900:          return "prime",       5
    elif 900 <= mins < 960:          return "power_hour",  5
    elif 960 <= mins < 1020:         return "afterhours", 15
    return "sleep", 60

def check_exit_conditions(positions):
    for pos in positions:
        symbol  = pos["symbol"]
        pnl_pct = float(pos["unrealized_plpc"])
        if pnl_pct >= RULES["take_profit_pct"]:
            log(f"🎯 Take profit {symbol} (+{pnl_pct*100:.1f}%)")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                # Remove from ownership tracking
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ SOLD {symbol} profit")
            except Exception as e: log(f"❌ {e}")
        elif pnl_pct <= -RULES["stop_loss_pct"]:
            log(f"🛑 Stop loss {symbol} ({pnl_pct*100:.1f}%)")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ SOLD {symbol} stop")
            except Exception as e: log(f"❌ {e}")

def build_full_context(equity, cash, positions, pos_details, chart_section, news, market_ctx):
    """Build the shared context both AIs receive"""
    return f"""=== REAL MONEY TRADING SYSTEM ===
Portfolio: ${equity:.2f} equity | ${cash:.2f} cash | Started: ${RULES['budget']}
P&L: ${equity - RULES['budget']:+.2f} ({(equity - RULES['budget'])/RULES['budget']*100:+.2f}%)

OPEN POSITIONS ({len(positions)}/{RULES['max_positions']}):
{chr(10).join(pos_details) if pos_details else '  None'}
Claude manages: {shared_state['claude_positions'] or 'none'}
Grok manages:   {shared_state['grok_positions'] or 'none'}
Autonomy mode:  {'YES — each AI trades independently' if shared_state['autonomy_mode'] else 'NO — collaboration required'}

MARKET CONDITIONS (SPY/QQQ/VIX):
{market_ctx}

LATEST NEWS (last 24h):
{news}

TECHNICAL INDICATORS (60-day daily bars + 5-day momentum):
{chart_section}

RULES:
- Stop loss: {RULES['stop_loss_pct']*100}% | Take profit: {RULES['take_profit_pct']*100}%
- Daily loss limit: {RULES['daily_loss_limit_pct']*100}% (${RULES['budget']*RULES['daily_loss_limit_pct']:.2f})
- Max positions: {RULES['max_positions']}
- Min confidence to trade: {RULES['min_confidence']}%
- Fee estimate: ~$0.01-0.05 per trade (SEC + FINRA fees)
- GOAL: Maximum profit. Fees matter — don't overtrade small positions."""

def collaborative_session(equity, cash, positions, pos_symbols, open_count, chart_section, news, market_ctx):
    """Full 3-round AI collaboration with news, trends and fee awareness"""

    pos_details = [
        f"  {p['symbol']}: entry=${float(p['avg_entry_price']):.2f} "
        f"now=${float(p['current_price']):.2f} "
        f"P&L={round(float(p['unrealized_plpc'])*100,2)}% "
        f"owner={'Claude' if p['symbol'] in shared_state['claude_positions'] else 'Grok' if p['symbol'] in shared_state['grok_positions'] else 'shared'}"
        for p in positions
    ]

    context = build_full_context(equity, cash, positions, pos_details, chart_section, news, market_ctx)

    # ── ROUND 1: Independent deep analysis ──────────────
    log("🔵 Round 1 — Claude: Deep analysis + strategy proposal...")
    log("🔴 Round 1 — Grok: News sentiment + momentum proposal...")

    r1_prompt = f"""{context}

You are proposing your BEST strategy for maximum profit right now.
Consider:
1. Technical indicators — which stocks have the strongest setups?
2. News sentiment — which news items are most market-moving?
3. Market conditions — is SPY/QQQ trending up or down? Adjust risk accordingly.
4. Fees — avoid trades under $10 notional where fees eat too much profit
5. Portfolio balance — don't overlap with existing positions unnecessarily
6. Growth path — if portfolio grows, what's the strategy evolution?

Propose up to 3 trades. Be specific about WHY each trade is profitable NOW.

Respond ONLY with JSON:
{{
  "market_thesis": "your read on overall market right now",
  "news_catalyst": "most important news item and its impact",
  "strategy_name": "name your approach",
  "proposed_trades": [
    {{
      "action": "buy/sell",
      "symbol": "TICKER",
      "notional_usd": 15.00,
      "confidence": 85,
      "signals": ["RSI=45 uptrend", "MACD positive", "news catalyst"],
      "rationale": "specific reason",
      "fee_estimate": 0.02,
      "net_profit_target": 1.05
    }}
  ],
  "risk_level": "low/medium/high",
  "autonomy_ready": true/false
}}"""

    claude_r1 = None
    grok_r1   = None

    try:
        claude_r1 = parse_json(ask_claude(r1_prompt,
            "You are Claude, a disciplined quant trader with deep technical analysis skills. You have access to news and market data. Respond ONLY with valid JSON."))
        if claude_r1:
            log(f"🔵 Claude thesis: {claude_r1.get('market_thesis','')[:100]}")
            log(f"🔵 Claude news catalyst: {claude_r1.get('news_catalyst','')[:100]}")
            log(f"🔵 Claude proposes {len(claude_r1.get('proposed_trades',[]))} trades via '{claude_r1.get('strategy_name','')}'")
    except Exception as e:
        log(f"❌ Claude R1: {e}")

    try:
        grok_r1 = parse_json(ask_grok(r1_prompt,
            "You are Grok, an aggressive momentum trader with real-time X/Twitter sentiment and news access. Use your knowledge of current trends. Respond ONLY with valid JSON."))
        if grok_r1:
            log(f"🔴 Grok thesis: {grok_r1.get('market_thesis','')[:100]}")
            log(f"🔴 Grok news catalyst: {grok_r1.get('news_catalyst','')[:100]}")
            log(f"🔴 Grok proposes {len(grok_r1.get('proposed_trades',[]))} trades via '{grok_r1.get('strategy_name','')}'")
    except Exception as e:
        log(f"❌ Grok R1: {e}")

    if not claude_r1 and not grok_r1:
        log("⚠️ Both AIs failed Round 1 — holding.")
        return [], False

    # ── ROUND 2: Cross-critique with news validation ─────
    log("🔵 Round 2 — Claude critiques Grok + validates with technicals...")
    log("🔴 Round 2 — Grok critiques Claude + validates with sentiment...")

    claude_r2 = None
    grok_r2   = None

    if claude_r1 and grok_r1:
        c_review = f"""{context}

YOUR analysis (Claude): {json.dumps(claude_r1)}
GROK's analysis: {json.dumps(grok_r1)}

Review Grok's proposals critically:
1. Do the technical indicators support Grok's news-driven picks?
2. Is Grok's sentiment read backed by the price action you see?
3. Are there overlapping stocks? If so, agree on ONE to avoid doubling up
4. Can you COMBINE strategies to cover more ground with the available cash?
5. Flag any trade where fees would eat >10% of expected profit
6. Is Grok ready for autonomy on any specific stocks?

Respond ONLY with JSON:
{{
  "technical_validation": "which of Grok's picks are technically sound",
  "sentiment_gaps": "where technicals contradict Grok's sentiment",
  "overlaps_resolved": "how you resolved any symbol overlaps",
  "combined_strategy": "merged approach description",
  "refined_trades": [
    {{"action":"buy/sell","symbol":"TICKER","notional_usd":15.00,"confidence":85,
      "signals":["s1","s2","s3"],"rationale":"merged reasoning","owner":"claude/grok/shared"}}
  ],
  "flag_high_fee_trades": ["TICKER if fees>10% profit"],
  "autonomy_recommendation": "which stocks each AI should own independently"
}}"""

        g_review = f"""{context}

YOUR analysis (Grok): {json.dumps(grok_r1)}
CLAUDE's analysis: {json.dumps(claude_r1)}

Review Claude's proposals with your sentiment lens:
1. Does current Twitter/X sentiment support Claude's technical picks?
2. Any breaking news that changes Claude's thesis?
3. Can you cover different stocks to maximize portfolio diversification?
4. Are there momentum plays Claude missed due to pure technical focus?
5. Which strategies show enough track record for autonomy?

Respond ONLY with JSON:
{{
  "sentiment_validation": "which of Claude's picks have positive sentiment",
  "news_contradictions": "any news that challenges Claude's analysis",
  "momentum_additions": "stocks Claude missed that have strong momentum now",
  "combined_strategy": "merged approach description",
  "refined_trades": [
    {{"action":"buy/sell","symbol":"TICKER","notional_usd":15.00,"confidence":85,
      "signals":["s1","s2","s3"],"rationale":"merged reasoning","owner":"claude/grok/shared"}}
  ],
  "autonomy_recommendation": "which stocks Grok should own independently"
}}"""

        try:
            claude_r2 = parse_json(ask_claude(c_review,
                "You are Claude validating a peer AI's trading strategy. Be critical and specific. Respond ONLY with valid JSON."))
            if claude_r2:
                log(f"🔵 Claude refined to {len(claude_r2.get('refined_trades',[]))} trades")
                log(f"🔵 Combined strategy: {claude_r2.get('combined_strategy','')[:100]}")
        except Exception as e:
            log(f"❌ Claude R2: {e}")

        try:
            grok_r2 = parse_json(ask_grok(g_review,
                "You are Grok validating a peer AI's trading strategy using sentiment and news. Respond ONLY with valid JSON."))
            if grok_r2:
                log(f"🔴 Grok refined to {len(grok_r2.get('refined_trades',[]))} trades")
                log(f"🔴 Momentum additions: {grok_r2.get('momentum_additions','')[:100]}")
        except Exception as e:
            log(f"❌ Grok R2: {e}")

    # ── ROUND 3: Final joint plan with autonomy check ────
    log("🤝 Round 3 — Building final joint plan + autonomy assessment...")

    c_trades = (claude_r2 or {}).get("refined_trades", (claude_r1 or {}).get("proposed_trades", []))
    g_trades = (grok_r2 or {}).get("refined_trades", (grok_r1 or {}).get("proposed_trades", []))
    all_owned = shared_state["claude_positions"] + shared_state["grok_positions"]

    final_prompt = f"""{context}

After 2 rounds of collaboration:
Claude's refined trades: {json.dumps(c_trades)}
Grok's refined trades:   {json.dumps(g_trades)}
Autonomy rec (Claude): {(claude_r2 or {}).get('autonomy_recommendation', 'not specified')}
Autonomy rec (Grok):   {(grok_r2 or {}).get('autonomy_recommendation', 'not specified')}
Currently owned symbols (avoid re-buying): {all_owned}

You are the FINAL DECISION MAKER. Create the optimal executable plan:

ALLOCATION RULES:
- Total notional <= ${cash * 0.95:.2f} (keep 5% cash buffer)
- Max {RULES['max_positions'] - open_count} new positions
- Minimum trade size: $8 (fees make smaller trades unprofitable)
- Prioritize trades both AIs agree on
- Solo trades allowed only at 90%+ confidence
- Assign clear OWNER (claude/grok/shared) to prevent future overlap
- Consider fee impact: estimate fees and ensure net profit target > 2x fees

AUTONOMY ASSESSMENT:
- If equity > ${RULES['autonomy_threshold']}: each AI can independently manage their owned stocks
- Current equity ${equity:.2f} {'→ AUTONOMY UNLOCKED' if equity >= RULES['autonomy_threshold'] else f'→ need ${RULES["autonomy_threshold"] - equity:.2f} more for autonomy'}

Respond ONLY with JSON:
{{
  "final_trades": [
    {{"action":"buy/sell","symbol":"TICKER","notional_usd":15.00,
      "confidence":85,"owner":"claude/grok/shared",
      "rationale":"specific merged reasoning",
      "fee_estimate":0.02,"net_profit_target":1.00}}
  ],
  "total_allocated": 30.00,
  "cash_remaining": 25.00,
  "autonomy_unlocked": true/false,
  "claude_autonomous_stocks": ["TICKER1"],
  "grok_autonomous_stocks": ["TICKER2"],
  "joint_message": "brief summary of the joint strategy"
}}"""

    try:
        final = parse_json(ask_claude(final_prompt,
            "You are the final trading decision maker synthesizing two AI strategies. Maximize profit while managing risk. Respond ONLY with valid JSON."))
        if final:
            log(f"🤝 Joint message: {final.get('joint_message','')[:150]}")
            log(f"🤝 Total allocated: ${final.get('total_allocated',0):.2f} | Cash remaining: ${final.get('cash_remaining',0):.2f}")
            for t in final.get("final_trades", []):
                fee = t.get('fee_estimate', 0)
                target = t.get('net_profit_target', 0)
                log(f"   {t.get('action','?').upper()} {t.get('symbol','?')} "
                    f"${t.get('notional_usd',0):.2f} | owner={t.get('owner','?')} "
                    f"conf={t.get('confidence','?')}% | fee≈${fee:.3f} target=${target:.2f}")
            autonomy = final.get("autonomy_unlocked", False)
            return final.get("final_trades", []), autonomy, final
        return [], False, {}
    except Exception as e:
        log(f"❌ Round 3: {e}")
        return [], False, {}

def execute_trades(final_trades, cash, pos_symbols, open_count, final_plan):
    """Execute trades and update ownership tracking"""
    remaining_cash = cash
    new_positions  = open_count

    # Update autonomy state
    if final_plan.get("autonomy_unlocked"):
        shared_state["autonomy_mode"] = True
        c_auto = final_plan.get("claude_autonomous_stocks", [])
        g_auto = final_plan.get("grok_autonomous_stocks", [])
        if c_auto: log(f"🔓 Claude gets autonomy over: {c_auto}")
        if g_auto: log(f"🔓 Grok gets autonomy over: {g_auto}")

    for trade in final_trades:
        action   = trade.get("action","hold").lower()
        symbol   = trade.get("symbol")
        notional = float(trade.get("notional_usd", 0))
        conf     = trade.get("confidence", 0)
        owner    = trade.get("owner", "shared")
        fee_est  = trade.get("fee_estimate", 0.02)

        if not symbol: continue

        if conf < RULES["min_confidence"]:
            log(f"⚠️ Skip {symbol} — conf {conf}% < {RULES['min_confidence']}%")
            continue

        if action == "buy":
            if new_positions >= RULES["max_positions"]:
                log(f"⚠️ Max positions — skip {symbol}")
                continue
            if symbol in pos_symbols:
                log(f"⚠️ Already own {symbol} — skip")
                continue
            if notional < 8:
                log(f"⚠️ {symbol} notional ${notional:.2f} too small (fees eat profit)")
                continue
            notional = min(notional, remaining_cash * 0.95)
            if notional < 8: continue

            try:
                order = alpaca("POST", "/v2/orders", {
                    "symbol": symbol, "notional": str(round(notional, 2)),
                    "side": "buy", "type": "market", "time_in_force": "day",
                })
                log(f"✅ REAL BUY {symbol} ${notional:.2f} | owner={owner} | fee≈${fee_est:.3f} | {order['id'][:8]}...")
                remaining_cash -= notional
                new_positions  += 1
                # Track ownership
                if owner == "claude" and symbol not in shared_state["claude_positions"]:
                    shared_state["claude_positions"].append(symbol)
                elif owner == "grok" and symbol not in shared_state["grok_positions"]:
                    shared_state["grok_positions"].append(symbol)
                pos_symbols.append(symbol)
            except Exception as e:
                log(f"❌ Buy {symbol}: {e}")

        elif action == "sell":
            if symbol not in pos_symbols:
                log(f"⚠️ No position in {symbol}")
                continue
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                log(f"✅ REAL SELL {symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                pos_symbols.remove(symbol)
            except Exception as e:
                log(f"❌ Sell {symbol}: {e}")

def run_cycle():
    log("── 🤝 AI Collaboration Cycle ──")
    if not is_market_open():
        log("Market closed.")
        return

    account   = alpaca("GET", "/v2/account")
    equity    = float(account["equity"])
    cash      = float(account["cash"])
    log(f"💰 REAL Equity: ${equity:.2f} | Cash: ${cash:.2f} | P&L: ${equity-RULES['budget']:+.2f}")

    loss_pct = (RULES["budget"] - equity) / RULES["budget"]
    if loss_pct >= RULES["daily_loss_limit_pct"]:
        log(f"🛑 Daily loss limit {loss_pct*100:.1f}% hit — STOPPING today.")
        return

    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)
    log(f"Positions ({open_count}): {pos_symbols or 'none'}")

    check_exit_conditions(positions)
    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)

    log("📡 Fetching news + market context...")
    news       = get_news_context()
    market_ctx = get_market_context()
    log("📊 Computing indicators...")
    chart_section = get_chart_section()

    final_trades, autonomy_unlocked, final_plan = collaborative_session(
        equity, cash, positions, pos_symbols, open_count, chart_section, news, market_ctx
    )

    if not final_trades:
        log("⏳ No trades agreed — holding.")
    else:
        execute_trades(final_trades, cash, pos_symbols, open_count, final_plan)

    shared_state["last_sync"] = datetime.now().isoformat()
    log("── Cycle complete ──\n")

def run_premarket():
    log("📊 PRE-MARKET: AIs building today's joint game plan...")
    news       = get_news_context()
    market_ctx = get_market_context()
    chart_section = get_chart_section()

    prompt = f"""Two AIs preparing for today's REAL money session.
Budget: ${RULES['budget']} | Equity goal: ${RULES['autonomy_threshold']} (autonomy threshold)

MARKET: {market_ctx}
NEWS: {news}
INDICATORS: {chart_section}

As a team, discuss:
1. What is today's dominant market narrative?
2. Which 3 stocks have the best risk/reward today?
3. What's the strategy to grow toward autonomy threshold?
4. Any news events to watch for during the session?

Plain text, 200 words max."""
    try:
        plan = ask_claude(prompt, "You are a collaborative trading analyst. Plain text response.")
        log(f"📋 Joint game plan:\n{plan[:600]}")
    except Exception as e:
        log(f"❌ Pre-market: {e}")

def run_afterhours():
    log("📈 AFTER-HOURS: Performance review + strategy evolution...")
    try:
        account   = alpaca("GET", "/v2/account")
        positions = alpaca("GET", "/v2/positions")
        equity    = float(account["equity"])
        pnl       = equity - RULES["budget"]
        pnl_pct   = pnl / RULES["budget"] * 100
        log(f"💰 REAL P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%) | Equity: ${equity:.2f}")
        log(f"📊 Autonomy mode: {'ACTIVE' if shared_state['autonomy_mode'] else f'locked (need ${RULES[\"autonomy_threshold\"]-equity:.2f} more)'}")
        log(f"📊 Claude owns: {shared_state['claude_positions'] or 'none'}")
        log(f"📊 Grok owns:   {shared_state['grok_positions'] or 'none'}")
        for p in positions:
            pnl_pct_p = round(float(p["unrealized_plpc"]) * 100, 2)
            owner = "Claude" if p["symbol"] in shared_state["claude_positions"] else "Grok" if p["symbol"] in shared_state["grok_positions"] else "Shared"
            log(f"   [{owner}] {p['symbol']}: {pnl_pct_p:+.2f}%")
        if not positions:
            log("✅ Fully in cash overnight — safe!")
    except Exception as e:
        log(f"❌ After-hours: {e}")

def trading_loop():
    log(f"🚀 COLLABORATIVE AI Trading System started!")
    log(f"💰 Real money budget: ${RULES['budget']} | Autonomy unlocks at: ${RULES['autonomy_threshold']}")
    log("🤝 Strategy: 3-round debate → news+technicals+sentiment → joint execution")
    log(f"🛡️ Safety: stop={RULES['stop_loss_pct']*100}% TP={RULES['take_profit_pct']*100}% daily_limit={RULES['daily_loss_limit_pct']*100}%")
    if not all([ALPACA_KEY, ALPACA_SECRET, ANTHROPIC_KEY, GROK_KEY]):
        log("❌ Missing env vars! Need: ALPACA_KEY, ALPACA_SECRET, ANTHROPIC_KEY, GROK_KEY")
        return

    last_premarket = None
    last_afterhours = None

    while True:
        try:
            mode, interval = get_market_mode()
            now_et = datetime.now(ZoneInfo("America/New_York"))
            today  = now_et.date()

            if mode == "sleep":
                next_check = (now_et + timedelta(minutes=interval)).strftime("%H:%M ET")
                log(f"😴 Sleeping {interval} min. Next check: {next_check}")

            elif mode == "premarket":
                if last_premarket != today:
                    run_premarket()
                    last_premarket = today
                else:
                    log("📊 Pre-market done. Waiting for open...")

            elif mode in ("opening", "prime", "power_hour"):
                labels = {"opening":"🔔 OPENING","prime":"🚀 PRIME","power_hour":"⚡ POWER HOUR"}
                log(f"{labels[mode]} — Starting collaboration")
                run_cycle()

            elif mode == "afterhours":
                if last_afterhours != today:
                    run_afterhours()
                    last_afterhours = today
                else:
                    log("📈 After-hours done.")

        except Exception as e:
            log(f"❌ Loop error: {e}")
            interval = 5

        mode, interval = get_market_mode()
        log(f"Sleeping {interval} min [mode: {mode}]...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    log(f"🌐 Proxy on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
