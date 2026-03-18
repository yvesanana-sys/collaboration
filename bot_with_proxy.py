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
    "total_budget":          55,
    "growth_reserve_pct":    0.15,   # Always keep 15% untouched for growth
    "trading_pool_pct":      0.85,   # 85% available to trade
    "daily_loss_limit_pct":  0.05,
    "max_positions":         5,
    "stop_loss_pct":         0.04,
    "take_profit_pct":       0.07,
    "min_confidence":          80,     # Autonomous trades
    "collab_min_confidence":   95,     # Collaborative big-ticket trades
    "collab_min_signals":      5,      # Signals required for collaborative
    "collab_require_news":     True,   # News catalyst required
    "collab_min_profit_pct":   0.05,   # Min expected profit (5%)
    # ── Collaborative big-ticket gatekeeper ──
    "collab_unlock_equity":    3000,   # Min equity to enable collaborative pool
    "collab_min_trade_size":   1000,   # Min trade size when collaborative active
    "collab_max_trade_pct":    0.40,   # Max 40% of collab pool per trade
    "interval_minutes":        5,
    # Feature unlock thresholds
    "short_sell_threshold":  2000,
    "options_threshold":     5000,
    "full_margin_threshold": 25000,
    # Performance allocation
    "base_split":            0.50,   # Start 50/50
    "max_allocation":        0.70,   # Winner gets max 70%
    "min_allocation":        0.30,   # Loser gets min 30%
    "performance_window":    7,      # Days to measure performance
    # Autonomy tiers — at each level, a portion splits off for AI autonomy
    # The autonomous_fund is the TOTAL set aside for both AIs at that tier
    # Each AI gets half of autonomous_fund (split by performance)
    # The REST stays in the collaborative pool
    # Example at $150: $50 autonomous ($25 Claude + $25 Grok) + $50 collaborative
    "autonomy_tiers": [
        {"equity": 150,  "autonomous_fund": 50,  "collab_floor": 50,  "description": "Tier 1 — $25 Claude + $25 Grok autonomous | $50+ collaborative"},
        {"equity": 300,  "autonomous_fund": 100, "collab_floor": 100, "description": "Tier 2 — $50 Claude + $50 Grok autonomous | $100+ collaborative"},
        {"equity": 600,  "autonomous_fund": 200, "collab_floor": 200, "description": "Tier 3 — $100 Claude + $100 Grok autonomous | $200+ collaborative"},
        {"equity": 1200, "autonomous_fund": 400, "collab_floor": 400, "description": "Tier 4 — $200 Claude + $200 Grok autonomous | $400+ collaborative"},
        {"equity": 2000, "autonomous_fund": 600, "collab_floor": 600, "description": "Tier 5 — $300 Claude + $300 Grok autonomous | $600+ collaborative + shorts"},
    ],
    "universe": [
        "NVDA","AMD","TSLA","META","AMZN",
        "PLTR","SOFI","MSTR","COIN","RKLB",
        "AAPL","MSFT","GOOGL","NFLX","CRM",
    ],
}

# ── Shared state ─────────────────────────────────────────
shared_state = {
    # Positions
    "claude_positions":    [],
    "grok_positions":      [],
    "bearish_watchlist":   [],
    # Fund allocation
    "claude_allocation":   0.50,   # Claude's share of trading pool
    "grok_allocation":     0.50,   # Grok's share of trading pool
    "growth_reserve":      0.0,    # Untouchable growth fund
    # Performance tracking
    "claude_daily_pnl":    0.0,
    "grok_daily_pnl":      0.0,
    "claude_weekly_pnl":   0.0,
    "grok_weekly_pnl":     0.0,
    "claude_total_pnl":    0.0,
    "grok_total_pnl":      0.0,
    "claude_win_days":     0,
    "grok_win_days":       0,
    "last_equity":         55.0,
    "day_start_equity":    55.0,
    "week_start_equity":   55.0,
    # Autonomy
    "autonomy_mode":        False,
    "autonomy_tier":        0,       # Current tier (0 = no autonomy)
    "claude_auto_fund":     0.0,     # Claude's autonomous fund
    "grok_auto_fund":       0.0,     # Grok's autonomous fund
    "last_sync":            None,
    "last_rebalance_day":   None,
    "last_rebalance_week":  None,
    # Today's joint research plan
    "todays_plan":          None,
    "todays_watchlist":     {},
    "tomorrows_plan":       None,
    "next_buy_target":      None,
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
    return jsonify({"status": "ok", "bot": BOT_NAME})

@app.route("/stats")
def stats():
    try:
        account   = alpaca_get("/v2/account")
        positions = alpaca_get("/v2/positions")
        equity    = float(account["equity"])
        features  = check_account_features(account, equity)
        pool      = get_trading_pool(equity)
        autonomy  = get_autonomy_status(equity)
        return jsonify({
            "bot":              BOT_NAME,
            "equity":           equity,
            "cash":             float(account["cash"]),
            "pnl":              round(equity - RULES["total_budget"], 2),
            "pnl_pct":          round((equity - RULES["total_budget"]) / RULES["total_budget"] * 100, 2),
            "mode":             "REAL",
            "growth_reserve":   round(pool["reserve"], 2),
            "trading_pool":     round(pool["trading"], 2),
            "claude_budget":    round(pool["claude"], 2),
            "grok_budget":      round(pool["grok"], 2),
            "claude_allocation": shared_state["claude_allocation"],
            "grok_allocation":   shared_state["grok_allocation"],
            "claude_daily_pnl":  shared_state["claude_daily_pnl"],
            "grok_daily_pnl":    shared_state["grok_daily_pnl"],
            "claude_weekly_pnl": shared_state["claude_weekly_pnl"],
            "grok_weekly_pnl":   shared_state["grok_weekly_pnl"],
            "claude_total_pnl":  shared_state["claude_total_pnl"],
            "grok_total_pnl":    shared_state["grok_total_pnl"],
            "can_short":         features["can_short"],
            "short_progress":    features["short_progress_pct"],
            "autonomy_mode":     shared_state["autonomy_mode"],
            "claude_owns":       shared_state["claude_positions"],
            "grok_owns":         shared_state["grok_positions"],
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

# ── Fund Allocation ──────────────────────────────────────
def get_trading_pool(equity):
    """
    Calculate fund pools — always protect growth reserve.

    Structure:
    ┌─────────────────────────────────────────┐
    │ Total Equity                             │
    ├─────────────────────────────────────────┤
    │ Growth Reserve (15%) — NEVER traded      │
    ├─────────────────────────────────────────┤
    │ Trading Pool (85%)                       │
    │  ├── Autonomous Claude (perf-based %)   │
    │  ├── Autonomous Grok   (perf-based %)   │
    │  └── Collaborative Pool (remainder)     │
    └─────────────────────────────────────────┘

    At $150 (Tier 1):
      $50 total autonomous → Claude $25 + Grok $25 (50/50 base)
      $50+ collaborative pool (continues as before)
    """
    reserve = equity * RULES["growth_reserve_pct"]
    trading = equity * RULES["trading_pool_pct"]

    if shared_state["autonomy_mode"] and shared_state["autonomy_tier"] > 0:
        tier_data  = RULES["autonomy_tiers"][shared_state["autonomy_tier"] - 1]
        total_auto = tier_data["autonomous_fund"]

        # Split autonomous fund by performance allocation
        c_alloc = shared_state["claude_allocation"]
        g_alloc = shared_state["grok_allocation"]
        c_auto  = round(total_auto * c_alloc, 2)
        g_auto  = round(total_auto * g_alloc, 2)

        # Collaborative pool = trading pool minus autonomous funds
        collab  = max(0, round(trading - total_auto, 2))

        return {
            "total":           equity,
            "reserve":         reserve,
            "trading":         trading,
            "autonomous_total": total_auto,
            "claude":          c_auto,    # Claude's independent budget
            "grok":            g_auto,    # Grok's independent budget
            "collaborative":   collab,    # Shared collaborative budget
            "autonomy_active": True,
            "tier":            shared_state["autonomy_tier"],
        }
    else:
        # No autonomy yet — full trading pool is collaborative
        claude = trading * shared_state["claude_allocation"]
        grok   = trading * shared_state["grok_allocation"]
        return {
            "total":           equity,
            "reserve":         reserve,
            "trading":         trading,
            "autonomous_total": 0,
            "claude":          claude,
            "grok":            grok,
            "collaborative":   trading,
            "autonomy_active": False,
            "tier":            0,
        }

def check_autonomy_tier(equity):
    """
    Check and update autonomy tier based on equity growth.
    Each tier gives each AI their own independent fund to manage.
    Performance determines who gets more of the autonomous allocation.
    """
    current_tier = shared_state["autonomy_tier"]
    tiers = RULES["autonomy_tiers"]

    # Find highest unlocked tier
    unlocked_tier = 0
    unlocked_data = None
    for i, tier in enumerate(tiers):
        if equity >= tier["equity"]:
            unlocked_tier = i + 1
            unlocked_data = tier

    if unlocked_tier > current_tier:
        # New tier unlocked!
        tier_data = tiers[unlocked_tier - 1]
        autonomous_fund = tier_data["autonomous_fund"]

        # Split autonomous fund based on performance
        c_alloc = shared_state["claude_allocation"]
        g_alloc = shared_state["grok_allocation"]
        # Each AI gets half of autonomous_fund, adjusted by performance
        half_fund = autonomous_fund / 2
        c_fund    = round(half_fund * (c_alloc / 0.5), 2)  # performance-adjusted from 50% base
        g_fund    = round(half_fund * (g_alloc / 0.5), 2)
        # Ensure total doesn't exceed autonomous_fund
        if c_fund + g_fund > autonomous_fund:
            c_fund = round(autonomous_fund * c_alloc, 2)
            g_fund = round(autonomous_fund - c_fund, 2)

        collab_pool = round((equity * RULES["trading_pool_pct"]) - autonomous_fund, 2)

        shared_state["autonomy_tier"]    = unlocked_tier
        shared_state["autonomy_mode"]    = True
        shared_state["claude_auto_fund"] = c_fund
        shared_state["grok_auto_fund"]   = g_fund

        log(f"🎉 AUTONOMY TIER {unlocked_tier} UNLOCKED! — {tier_data['description']}")
        log(f"🔵 Claude autonomous fund: ${c_fund:.2f} ({c_alloc*100:.0f}% performance share)")
        log(f"🔴 Grok autonomous fund:   ${g_fund:.2f} ({g_alloc*100:.0f}% performance share)")
        log(f"🤝 Collaborative pool continues: ${collab_pool:.2f} (both AIs trade this together)")
        log(f"💡 Structure: ${c_fund:.2f} Claude + ${g_fund:.2f} Grok + ${collab_pool:.2f} collaborative")

        # Next tier preview
        if unlocked_tier < len(tiers):
            next_tier = tiers[unlocked_tier]
            needed    = next_tier["equity"] - equity
            log(f"🎯 Next tier: ${next_tier['equity']} (need ${needed:.2f} more) → ${next_tier['autonomous_fund']} each AI")

        return True, tier_data
    return False, unlocked_data

def get_autonomy_status(equity):
    """Get current autonomy status and next milestone"""
    tiers = RULES["autonomy_tiers"]
    current_tier = shared_state["autonomy_tier"]

    if current_tier == 0:
        next_tier = tiers[0]
        needed    = next_tier["equity"] - equity
        progress  = round(equity / next_tier["equity"] * 100, 1)
        return {
            "tier": 0,
            "active": False,
            "claude_fund": 0,
            "grok_fund": 0,
            "next_milestone": next_tier["equity"],
            "next_fund": next_tier["autonomous_fund"],
            "needed": round(needed, 2),
            "progress_pct": progress,
            "description": f"Need ${needed:.2f} more for Tier 1 (${next_tier['autonomous_fund']} each AI)",
        }

    tier_data = tiers[current_tier - 1]
    if current_tier < len(tiers):
        next_tier = tiers[current_tier]
        needed    = next_tier["equity"] - equity
        progress  = round(equity / next_tier["equity"] * 100, 1)
        next_desc = f"${needed:.2f} to Tier {current_tier+1} (${next_tier['autonomous_fund']} each)"
    else:
        needed    = 0
        progress  = 100
        next_desc = "Max tier reached!"

    return {
        "tier": current_tier,
        "active": True,
        "claude_fund": shared_state["claude_auto_fund"],
        "grok_fund": shared_state["grok_auto_fund"],
        "next_milestone": tiers[current_tier]["equity"] if current_tier < len(tiers) else None,
        "needed": round(needed, 2),
        "progress_pct": progress,
        "description": tier_data["description"],
        "next_description": next_desc,
    }

def rebalance_autonomy_funds(equity):
    """Rebalance autonomous funds based on updated performance allocation"""
    if shared_state["autonomy_tier"] == 0:
        return
    tier_data       = RULES["autonomy_tiers"][shared_state["autonomy_tier"] - 1]
    autonomous_fund = tier_data["autonomous_fund"]
    c_alloc         = shared_state["claude_allocation"]
    g_alloc         = shared_state["grok_allocation"]
    c_fund          = round(autonomous_fund * c_alloc, 2)
    g_fund          = round(autonomous_fund - c_fund, 2)
    collab          = round((equity * RULES["trading_pool_pct"]) - autonomous_fund, 2)
    shared_state["claude_auto_fund"] = c_fund
    shared_state["grok_auto_fund"]   = g_fund
    log(f"⚖️ Autonomous rebalanced: Claude=${c_fund:.2f} | Grok=${g_fund:.2f} | Collaborative=${collab:.2f}")

def rebalance_allocations(daily=True):
    """
    Rebalance fund allocation based on performance.
    Winner gets more funds, loser gets less.
    Performance window: daily + weekly.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today  = now_et.date()
    week   = now_et.isocalendar()[1]

    c_pnl = shared_state["claude_daily_pnl"] if daily else shared_state["claude_weekly_pnl"]
    g_pnl = shared_state["grok_daily_pnl"]   if daily else shared_state["grok_weekly_pnl"]
    period = "daily" if daily else "weekly"

    log(f"⚖️ {period.upper()} REBALANCE — Claude: ${c_pnl:+.2f} | Grok: ${g_pnl:+.2f}")

    total_pnl = c_pnl + g_pnl

    if total_pnl <= 0:
        # Both lost — reset to 50/50, preserve capital
        log("📊 Both negative — resetting to 50/50 split")
        shared_state["claude_allocation"] = RULES["base_split"]
        shared_state["grok_allocation"]   = RULES["base_split"]
        return

    if total_pnl == 0 or (c_pnl == 0 and g_pnl == 0):
        log("📊 No performance data — keeping current split")
        return

    # Calculate performance-based allocation
    if c_pnl >= 0 and g_pnl >= 0:
        # Both profitable — allocate proportionally
        total = c_pnl + g_pnl
        c_share = c_pnl / total if total > 0 else 0.5
        g_share = g_pnl / total if total > 0 else 0.5
    elif c_pnl >= 0 and g_pnl < 0:
        # Claude wins
        c_share = RULES["max_allocation"]
        g_share = RULES["min_allocation"]
        shared_state["claude_win_days"] += 1
    elif g_pnl >= 0 and c_pnl < 0:
        # Grok wins
        c_share = RULES["min_allocation"]
        g_share = RULES["max_allocation"]
        shared_state["grok_win_days"] += 1
    else:
        c_share = 0.5
        g_share = 0.5

    # Clamp to min/max
    c_share = max(RULES["min_allocation"], min(RULES["max_allocation"], c_share))
    g_share = 1.0 - c_share

    old_c = shared_state["claude_allocation"]
    old_g = shared_state["grok_allocation"]

    shared_state["claude_allocation"] = round(c_share, 3)
    shared_state["grok_allocation"]   = round(g_share, 3)

    # Update total PnL tracking
    shared_state["claude_total_pnl"] += c_pnl
    shared_state["grok_total_pnl"]   += g_pnl

    winner = "Claude 🔵" if c_share > g_share else "Grok 🔴" if g_share > c_share else "Tied"
    log(f"🏆 {period.upper()} WINNER: {winner}")
    log(f"💰 New allocation: Claude={c_share*100:.1f}% → Grok={g_share*100:.1f}%")
    log(f"   (was Claude={old_c*100:.1f}% → Grok={old_g*100:.1f}%)")

    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])
    pool    = get_trading_pool(equity)
    log(f"💼 New budgets: Claude=${pool['claude']:.2f} | Grok=${pool['grok']:.2f} | Reserve=${pool['reserve']:.2f} (untouched)")

    # Rebalance autonomous funds too
    try:
        account = alpaca("GET", "/v2/account")
        equity  = float(account["equity"])
        rebalance_autonomy_funds(equity)
    except:
        pass

    # Reset daily PnL after rebalance
    if daily:
        shared_state["claude_daily_pnl"] = 0.0
        shared_state["grok_daily_pnl"]   = 0.0
        shared_state["last_rebalance_day"] = today
    else:
        shared_state["claude_weekly_pnl"] = 0.0
        shared_state["grok_weekly_pnl"]   = 0.0
        shared_state["last_rebalance_week"] = week

def track_pnl(positions):
    """Update P&L tracking per AI based on owned positions"""
    c_pnl = sum(float(p["unrealized_pl"]) for p in positions
                if p["symbol"] in shared_state["claude_positions"])
    g_pnl = sum(float(p["unrealized_pl"]) for p in positions
                if p["symbol"] in shared_state["grok_positions"])
    shared_state["claude_daily_pnl"]  = round(c_pnl, 2)
    shared_state["grok_daily_pnl"]    = round(g_pnl, 2)
    shared_state["claude_weekly_pnl"] = round(shared_state.get("claude_weekly_pnl", 0) + c_pnl, 2)
    shared_state["grok_weekly_pnl"]   = round(shared_state.get("grok_weekly_pnl", 0) + g_pnl, 2)

# ── Account Features ─────────────────────────────────────
def check_account_features(account, equity=None):
    if equity is None:
        equity = float(account.get("equity", 0))
    shorting_enabled = account.get("shorting_enabled", False)
    features = {
        "can_short":          shorting_enabled and equity >= RULES["short_sell_threshold"],
        "is_margin":          account.get("account_type", "cash") == "margin",
        "equity":             equity,
        "shorting_enabled":   shorting_enabled,
        "short_progress_pct": round(min(equity / RULES["short_sell_threshold"] * 100, 100), 1),
        "until_short":        max(0, round(RULES["short_sell_threshold"] - equity, 2)),
    }
    short_status = "✅" if features["can_short"] else f"🔒 ${features['until_short']:.0f} away"
    log(f"📋 Features: shorts={short_status} | equity=${equity:.2f} ({features['short_progress_pct']}% to $2k)")
    return features

# ── Market Data ──────────────────────────────────────────
def get_bars(symbol, days=60):
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=days+10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&feed=iex&adjustment=raw"
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            url2 = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&adjustment=raw"
            res  = requests.get(url2, headers=headers, timeout=10)
        if res.ok:
            bars = res.json().get("bars", [])
            if bars: return bars
        return []
    except Exception as e:
        log(f"⚠️ Bars {symbol}: {e}")
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
    mom_5d=round((closes[-1]-closes[-6])/closes[-6]*100,2) if len(closes)>=6 else None
    return {"close":round(close,2),"rsi":rsi_v,"macd":macd,
            "sma20":round(sma20,2) if sma20 else None,
            "sma50":round(sma50,2) if sma50 else None,
            "ema9":round(ema9,2),"ema21":round(ema21,2),
            "bb_pct":bb_pct,"vol_ratio":vol_ratio,"mom_5d":mom_5d}

def get_chart_section():
    lines = []
    for sym in RULES["universe"]:
        bars=get_bars(sym); ind=compute_indicators(bars)
        if not ind: lines.append(f"  {sym}: insufficient data"); continue
        lines.append(f"  {sym}: ${ind['close']} RSI={ind['rsi']} MACD={ind['macd']} "
                     f"SMA20={ind['sma20']} SMA50={ind['sma50']} EMA9={ind['ema9']} "
                     f"EMA21={ind['ema21']} BB%={ind['bb_pct']} Vol={ind['vol_ratio']} Mom5d={ind['mom_5d']}%")
    return "\n".join(lines)

def get_full_market_intelligence():
    """
    Gather ALL market intelligence:
    - Technical indicators
    - News (24h)
    - Politician trades (public disclosure)
    - Top investor portfolios (13F filings)
    - Biggest gainers today
    - Smart money analysis (combined scoring)
    """
    log("📡 Gathering full market intelligence...")
    chart_section = get_chart_section()
    news          = get_news_context()
    market_ctx    = get_market_context()

    log("🏛️ Fetching politician trades...")
    pol_text, pol_trades = get_politician_trades()
    pol_signals   = analyze_politician_signals(pol_trades, chart_section)

    log("💼 Fetching top investor portfolios...")
    inv_text, inv_holdings = get_top_investor_portfolios()

    log("📈 Fetching biggest gainers...")
    gainers = get_biggest_gainers()

    log("🆕 Detecting recent IPOs...")
    ipos = get_recent_ipos()

    log("🧠 Running smart money analysis...")
    smart_money = analyze_smart_money(pol_signals, inv_holdings, gainers)

    if smart_money["triple_confirmation"]:
        log(f"🔥 TRIPLE CONFIRMATION stocks: {smart_money['triple_confirmation']}")
    if smart_money["top_collab"]:
        log(f"⭐ Top collaborative candidates: {smart_money['top_collab']}")

    return {
        "chart_section": chart_section,
        "news":          news,
        "market_ctx":    market_ctx,
        "pol_text":      pol_text,
        "pol_trades":    pol_trades,
        "pol_signals":   pol_signals,
        "inv_text":      inv_text,
        "inv_holdings":  inv_holdings,
        "gainers":       gainers,
        "ipos":          ipos,
        "smart_money":   smart_money,
    }

def get_news_context():
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        symbols = ",".join(RULES["universe"][:8])
        url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbols}&start={start}&end={end}&limit=15&sort=desc"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            articles = res.json().get("news", [])
            lines = []
            for a in articles[:12]:
                sym = a.get("symbols", ["?"])[0] if a.get("symbols") else "MKT"
                lines.append(f"  [{sym}] {a.get('headline','')}")
            return "\n".join(lines) if lines else "  No news"
        return "  News unavailable"
    except Exception as e:
        return f"  News error: {e}"

def get_market_context():
    try:
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = f"{DATA_URL}/v2/stocks/snapshots?symbols=SPY,QQQ"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            data = res.json()
            lines = []
            for sym, snap in data.items():
                bar  = snap.get("dailyBar", {})
                prev = snap.get("prevDailyBar", {})
                if bar and prev:
                    chg = round((bar.get("c",0)-prev.get("c",0))/prev.get("c",1)*100,2)
                    lines.append(f"  {sym}: ${bar.get('c',0):.2f} ({'+' if chg>=0 else ''}{chg}%)")
            return "\n".join(lines) if lines else "  Market data unavailable"
        return "  Market unavailable"
    except Exception as e:
        return f"  Market error: {e}"


def get_politician_trades():
    """
    Fetch recent politician stock trades from public disclosure APIs.
    Uses quiverquant or capitoltrades public data.
    Politicians must disclose trades within 45 days — we track the biggest movers.
    """
    try:
        # Try Capitol Trades public API (free, no auth needed)
        url = "https://api.capitoltrades.com/trades?pageSize=20&sortBy=-publishedAt"
        res = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        if res.ok:
            data  = res.json()
            items = data.get("data", [])
            trades = []
            for t in items[:15]:
                politician = t.get("politician", {}).get("name", "Unknown")
                party      = t.get("politician", {}).get("party", "?")
                chamber    = t.get("politician", {}).get("chamber", "?")
                ticker     = t.get("asset", {}).get("ticker", "")
                asset_name = t.get("asset", {}).get("name", "")
                action     = t.get("type", "")      # buy/sell
                size       = t.get("size", "")      # $1k-$5k, $5k-$25k etc
                filed_date = t.get("publishedAt", "")[:10]
                if ticker and ticker in RULES["universe"] + ["SPY","QQQ","NVDA","AAPL","MSFT"]:
                    trades.append({
                        "politician": politician,
                        "party":      party,
                        "ticker":     ticker,
                        "action":     action,
                        "size":       size,
                        "filed":      filed_date,
                    })
            if trades:
                lines = [f"  [{t['party']}] {t['politician']}: {t['action'].upper()} {t['ticker']} {t['size']} ({t['filed']})"
                         for t in trades]
                return "\n".join(lines), trades
        return "  Capitol Trades API unavailable", []
    except Exception as e:
        log(f"⚠️ Capitol Trades fetch failed: {e}")

    # Fallback: Try QuiverQuant congress trading (free tier)
    try:
        url = "https://api.quiverquant.com/beta/live/congresstrading"
        res = requests.get(url, timeout=10)
        if res.ok:
            items  = res.json()[:15]
            trades = []
            for t in items:
                ticker = t.get("Ticker", "")
                if ticker:
                    trades.append({
                        "politician": t.get("Representative", "Unknown"),
                        "party":      t.get("Party", "?"),
                        "ticker":     ticker,
                        "action":     t.get("Transaction", ""),
                        "size":       t.get("Range", ""),
                        "filed":      t.get("ReportDate", ""),
                    })
            if trades:
                lines = [f"  [{t['party']}] {t['politician']}: {t['action'].upper()} {t['ticker']} {t['size']} ({t['filed']})"
                         for t in trades]
                return "\n".join(lines), trades
    except Exception as e:
        log(f"⚠️ QuiverQuant fetch failed: {e}")

    return "  Politician trade data unavailable", []

def analyze_politician_signals(trades, chart_section):
    """
    Analyze politician trades for mimicking opportunities.
    Focus on:
    1. Stocks multiple politicians are buying (strong signal)
    2. Stocks in our universe that politicians are buying
    3. Committee members buying stocks in their oversight area
    4. Recent buys (within 30 days) — most actionable
    """
    if not trades:
        return {}

    # Count buys per ticker
    buy_counts  = {}
    sell_counts = {}
    for t in trades:
        ticker = t.get("ticker", "")
        action = t.get("action", "").lower()
        if not ticker: continue
        if "buy" in action or "purchase" in action:
            buy_counts[ticker]  = buy_counts.get(ticker, 0) + 1
        elif "sell" in action or "sale" in action:
            sell_counts[ticker] = sell_counts.get(ticker, 0) + 1

    # Find strongest signals
    signals = {}
    for ticker, count in sorted(buy_counts.items(), key=lambda x: -x[1]):
        signals[ticker] = {
            "action":      "buy",
            "count":       count,
            "strength":    "STRONG" if count >= 3 else "MODERATE" if count >= 2 else "WEAK",
            "in_universe": ticker in RULES["universe"],
            "mimick_score": count * (2 if ticker in RULES["universe"] else 1),
        }
    for ticker, count in sell_counts.items():
        if ticker not in signals:
            signals[ticker] = {
                "action":      "sell",
                "count":       count,
                "strength":    "STRONG" if count >= 3 else "MODERATE" if count >= 2 else "WEAK",
                "in_universe": ticker in RULES["universe"],
                "mimick_score": count,
            }

    # Top mimick candidates — in universe AND being bought
    top_mimick = [
        t for t, d in sorted(signals.items(), key=lambda x: -x[1]["mimick_score"])
        if d["action"] == "buy" and d["in_universe"]
    ][:3]

    return {
        "buy_signals":    {t: d for t, d in signals.items() if d["action"] == "buy"},
        "sell_signals":   {t: d for t, d in signals.items() if d["action"] == "sell"},
        "top_mimick":     top_mimick,
        "universe_buys":  [t for t in top_mimick if t in RULES["universe"]],
    }

def get_biggest_gainers():
    """
    Fetch today's biggest gainers from Alpaca screener.
    Only used for collaborative consideration — not autonomous trades.
    """
    try:
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url     = f"{DATA_URL}/v1beta1/screener/stocks/movers?top=20&market_type=stocks"
        res     = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            gainers = res.json().get("gainers", [])
            # Filter: must be in universe OR be high quality stock
            top = []
            for g in gainers[:10]:
                sym = g.get("symbol","")
                pct = float(g.get("percent_change", 0))
                if pct > 3.0:  # Only stocks up 3%+ today
                    top.append({
                        "symbol":  sym,
                        "change":  pct,
                        "in_universe": sym in RULES["universe"],
                    })
            if top:
                log(f"📈 Biggest gainers today (>3%): {[(t['symbol'], f'+{t["change"]:.1f}%') for t in top]}")
            return top
    except Exception as e:
        log(f"⚠️ Gainers fetch failed: {e}")
    return []



def get_recent_ipos(min_days=30, max_days=180):
    """
    Fetch recent IPOs with 30-180 days of trading history.
    IPOs often have explosive momentum — good for autonomous AND collaborative.
    Requirements: 500k+ avg volume, $5-$200 price range.
    """
    try:
        end     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start   = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

        # Get all active tradable assets
        res = requests.get(f"{BASE_URL}/v2/assets?status=active&asset_class=us_equity",
                          headers=headers, timeout=10)
        if not res.ok:
            return []

        assets = res.json()
        candidates = [
            a["symbol"] for a in assets
            if a.get("tradable") and a.get("marginable") and a.get("easy_to_borrow")
            and not a.get("symbol","").endswith(("W","R","U","P"))
            and len(a.get("symbol","")) <= 5
        ]

        recent_ipos = []
        import random
        sample = random.sample(candidates, min(200, len(candidates)))

        for sym in sample:
            try:
                url = f"{DATA_URL}/v2/stocks/{sym}/bars?timeframe=1Day&start={start}&end={end}&limit=200&feed=iex"
                r   = requests.get(url, headers=headers, timeout=5)
                if r.ok:
                    bars = r.json().get("bars", [])
                    if min_days <= len(bars) <= max_days:
                        avg_vol    = sum(b["v"] for b in bars) / len(bars)
                        last_price = bars[-1]["c"]
                        mom_5d     = round((bars[-1]["c"] - bars[-6]["c"]) / bars[-6]["c"] * 100, 2) if len(bars) >= 6 else 0
                        if avg_vol > 500000 and 5 <= last_price <= 200:
                            recent_ipos.append({
                                "symbol":   sym,
                                "days_old": len(bars),
                                "price":    last_price,
                                "avg_vol":  round(avg_vol),
                                "mom_5d":   mom_5d,
                            })
                            if len(recent_ipos) >= 8:
                                break
            except:
                continue

        if recent_ipos:
            # Sort by momentum
            recent_ipos = sorted(recent_ipos, key=lambda x: -abs(x["mom_5d"]))
            log(f"🆕 Recent IPOs detected ({len(recent_ipos)}): {[i['symbol'] for i in recent_ipos]}")

        return recent_ipos
    except Exception as e:
        log(f"⚠️ IPO detection failed: {e}")
        return []

# ── Top Investor / Fund Tracking ─────────────────────────
# SEC CIK numbers for top investors (public 13F filings)
TOP_INVESTORS = {
    "Cathie Wood (ARK)":      "0001697748",
    "Michael Burry":          "0001649339",
    "Warren Buffett":         "0001067983",
    "George Soros":           "0001029160",
    "Ray Dalio (Bridgewater)":"0001350694",
    "Bill Ackman (Pershing)": "0001336528",
    "David Tepper":           "0001262463",
    "Stanley Druckenmiller":  "0001536411",
}

def get_sec_13f_holdings(cik, investor_name):
    """Fetch latest 13F filing from SEC EDGAR for an investor"""
    try:
        # Get latest filings
        url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
        res = requests.get(url, timeout=10,
                          headers={"User-Agent": "TradingBot research@example.com"})
        if not res.ok:
            return []

        data    = res.json()
        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        acc_nos = filings.get("accessionNumber", [])
        dates   = filings.get("filingDate", [])

        # Find most recent 13F-HR
        for i, form in enumerate(forms):
            if form == "13F-HR":
                acc_no   = acc_nos[i].replace("-","")
                fil_date = dates[i]
                # Get the filing index
                idx_url  = f"https://www.sec.gov/Archives/edgar/full-index/2024/QTR4/form.idx"
                doc_url  = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=1&search_text="
                # Parse holdings from the filing
                holding_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/infotable.xml"
                h_res = requests.get(holding_url, timeout=10,
                                    headers={"User-Agent": "TradingBot research@example.com"})
                if h_res.ok:
                    import re
                    holdings = []
                    # Parse XML for stock holdings
                    tickers_found = re.findall(r"<nameOfIssuer>([^<]+)</nameOfIssuer>", h_res.text)
                    values_found  = re.findall(r"<value>([^<]+)</value>", h_res.text)
                    shares_found  = re.findall(r"<sshPrnamt>([^<]+)</sshPrnamt>", h_res.text)
                    for j, name in enumerate(tickers_found[:20]):
                        val = int(values_found[j]) * 1000 if j < len(values_found) else 0
                        holdings.append({
                            "name":       name.strip(),
                            "value_usd":  val,
                            "investor":   investor_name,
                            "filed":      fil_date,
                        })
                    return sorted(holdings, key=lambda x: -x["value_usd"])[:10]
                break
    except Exception as e:
        pass
    return []

def get_top_investor_portfolios():
    """
    Track portfolios of the world's best investors via SEC 13F filings.
    Returns their top holdings and recent changes.
    Focus on stocks in our universe.
    """
    log("💼 Fetching top investor portfolios (SEC 13F)...")
    all_holdings = {}
    universe_set = set(RULES["universe"])

    # Use a faster approach — fetch from a free aggregator
    try:
        # WhaleWisdom free API (top holdings aggregated)
        whale_holdings = {}

        # Check each investor
        for investor, cik in list(TOP_INVESTORS.items())[:4]:  # limit to 4 to avoid timeout
            holdings = get_sec_13f_holdings(cik, investor)
            for h in holdings[:5]:
                name = h["name"].upper()
                # Try to match to our universe
                for sym in universe_set:
                    if sym in name or name in sym:
                        if sym not in all_holdings:
                            all_holdings[sym] = []
                        all_holdings[sym].append({
                            "investor": investor,
                            "value":    h["value_usd"],
                            "filed":    h["filed"],
                        })

        if all_holdings:
            lines = []
            for sym, holders in sorted(all_holdings.items(),
                                       key=lambda x: sum(h["value"] for h in x[1]), reverse=True):
                total_val = sum(h["value"] for h in holders)
                investors = [h["investor"].split("(")[0].strip() for h in holders]
                lines.append(f"  {sym}: held by {', '.join(investors)} | total ${total_val/1e6:.1f}M")
            log(f"💼 Universe stocks held by top investors:")
            for l in lines: log(l)
            return "\n".join(lines), all_holdings

    except Exception as e:
        log(f"⚠️ SEC 13F fetch error: {e}")

    # Fallback: use hardcoded known major holdings (updated quarterly)
    log("💼 Using cached top investor holdings...")
    known_holdings = {
        "AAPL": ["Warren Buffett (largest position ~$170B)", "Many funds"],
        "NVDA": ["Cathie Wood ARK (top holding)", "Many growth funds"],
        "MSFT": ["Bill Ackman", "Many value funds"],
        "AMZN": ["George Soros", "Many growth funds"],
        "META": ["Stanley Druckenmiller (recent buy)", "Many tech funds"],
        "TSLA": ["Cathie Wood ARK (core position)", "Many growth funds"],
        "GOOGL": ["Warren Buffett (recent add)", "Many value funds"],
        "PLTR": ["Cathie Wood ARK (large position)", "Growth funds"],
    }

    lines = []
    universe_overlap = {k: v for k, v in known_holdings.items() if k in universe_set}
    for sym, holders in universe_overlap.items():
        lines.append(f"  {sym}: {holders[0]}")

    return "\n".join(lines), {sym: [{"investor": h, "value": 0, "filed": "cached"}]
                                for sym, holders in universe_overlap.items()
                                for h in holders}

def analyze_smart_money(pol_signals, investor_holdings, gainers):
    """
    Combine politician trades + top investor holdings + biggest gainers
    to find the STRONGEST collaborative signals.

    Triple confirmation = politician buy + top investor holds + biggest gainer today
    """
    universe_set = set(RULES["universe"])
    scores = {}

    # Score each universe stock
    for sym in universe_set:
        score = 0
        reasons = []

        # Politician signal (+3 per politician buying)
        pol_buy = pol_signals.get("buy_signals", {}).get(sym, {})
        if pol_buy:
            pol_count = pol_buy.get("count", 0)
            score    += pol_count * 3
            reasons.append(f"{pol_count} politician(s) buying")

        # Top investor holding (+2 per investor)
        inv_holders = investor_holdings.get(sym, [])
        if inv_holders:
            score += len(inv_holders) * 2
            names  = [h.get("investor","").split("(")[0].strip() for h in inv_holders[:2]]
            reasons.append(f"held by {', '.join(names)}")

        # Biggest gainer today (+4 — most immediate signal)
        gainer_data = next((g for g in gainers if g.get("symbol") == sym), None)
        if gainer_data and gainer_data.get("change", 0) > 3:
            score += 4
            reasons.append(f"biggest gainer +{gainer_data['change']:.1f}% today")

        if score > 0:
            scores[sym] = {
                "score":         score,
                "reasons":       reasons,
                "is_triple":     score >= 9,  # All 3 signals
                "is_double":     score >= 5,  # 2 signals
                "collab_worthy": score >= 5,  # Recommend for collaboration
            }

    # Sort by score
    ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])

    if ranked:
        log("🧠 Smart money analysis:")
        for sym, data in ranked[:5]:
            tag = "🔥 TRIPLE" if data["is_triple"] else "⭐ DOUBLE" if data["is_double"] else "📌"
            log(f"   {tag} {sym}: score={data['score']} — {' | '.join(data['reasons'])}")

    return {
        "ranked":     ranked,
        "top_collab": [sym for sym, d in ranked if d["collab_worthy"]][:3],
        "triple_confirmation": [sym for sym, d in ranked if d["is_triple"]],
    }

def estimate_fees(notional):
    return round(max(notional * 0.0000278, 0.01) + min(notional * 0.000145, 7.27), 4)

# ── AI Calls ─────────────────────────────────────────────
def ask_claude(prompt, system="You are a trading AI. Respond with ONLY a short valid JSON object under 500 characters. No markdown, no explanations, no extra text.", max_tokens=600):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["content"][0]["text"]

def ask_grok(prompt, system="You are a trading AI. Respond with ONLY a short valid JSON object under 500 characters. No markdown, no explanations, no extra text.", max_tokens=600):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-3-mini", "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["choices"][0]["message"]["content"]

def clean_json_str(raw):
    import re
    raw = re.sub(r"```\w*", "", raw).replace("```","").strip()
    raw = "".join(ch for ch in raw if ord(ch) >= 32 or ch in "\n\t")
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    first = min(raw.find("{") if raw.find("{")!=-1 else len(raw),
                raw.find("[") if raw.find("[")!=-1 else len(raw))
    if first > 0: raw = raw[first:]
    last = max(raw.rfind("}"), raw.rfind("]"))
    if last != -1: raw = raw[:last+1]
    return raw.strip()

def parse_json(raw):
    try:
        raw = clean_json_str(raw)
        s=raw.find("{"); e=raw.rfind("}")+1
        if s==-1 or e==0: return None
        json_str = raw[s:e]
        try: return json.loads(json_str)
        except json.JSONDecodeError:
            last_comma = json_str.rfind(",")
            if last_comma > 0:
                try: return json.loads(json_str[:last_comma]+"}")
                except: pass
        return None
    except: return None

def parse_json_list(raw):
    try:
        raw = clean_json_str(raw)
        s=raw.find("["); e=raw.rfind("]")+1
        if s==-1 or e==0:
            obj=parse_json(raw)
            return [obj] if obj else []
        return json.loads(raw[s:e])
    except:
        obj=parse_json(raw)
        return [obj] if obj else []

def ask_with_retry(ask_fn, prompt, system, retries=3):
    for attempt in range(retries+1):
        try:
            raw    = ask_fn(prompt, system)
            result = parse_json(raw)
            if result: return result
            if attempt < retries:
                log(f"⚠️ JSON parse failed attempt {attempt+1}, raw: {raw[:100]}")
                time.sleep(2)
            else:
                log(f"⚠️ All retries failed. Raw: {raw[:200]}")
        except Exception as e:
            if attempt < retries:
                log(f"⚠️ API error {attempt+1}: {e}")
                time.sleep(3)
            else:
                log(f"❌ Final error: {e}")
    return None

# ── Market Schedule ──────────────────────────────────────
def is_market_open():
    return alpaca("GET", "/v2/clock").get("is_open", False)

def get_market_mode():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins   = now_et.hour * 60 + now_et.minute
    if mins < 510 or mins >= 1020:   return "sleep",      60
    elif 510 <= mins < 570:          return "premarket",  20
    elif 570 <= mins < 630:          return "opening",     5
    elif 630 <= mins < 900:          return "prime",       5
    elif 900 <= mins < 960:          return "power_hour",  5
    elif 960 <= mins < 1020:         return "afterhours", 20
    return "sleep", 60

# ── Exit Conditions ──────────────────────────────────────
def check_exit_conditions(positions):
    for pos in positions:
        symbol  = pos["symbol"]
        pnl_pct = float(pos["unrealized_plpc"])
        owner   = "Claude" if symbol in shared_state["claude_positions"] else "Grok"
        if pnl_pct >= RULES["take_profit_pct"]:
            log(f"🎯 [{owner}] Take profit {symbol} (+{pnl_pct*100:.1f}%)")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ SOLD {symbol} profit")
            except Exception as e: log(f"❌ {e}")
        elif pnl_pct <= -RULES["stop_loss_pct"]:
            log(f"🛑 [{owner}] Stop loss {symbol} ({pnl_pct*100:.1f}%)")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ SOLD {symbol} stop")
            except Exception as e: log(f"❌ {e}")

# ── Collaboration Engine ─────────────────────────────────

def is_collaborative_trade_worthy(trade_claude, trade_grok, chart_section, news, equity=0, collab_pool=0):
    """
    Gate-keeper for collaborative big-ticket trades.
    LOCKED until equity >= $3,000 and min trade size $1,000.
    Requires: 95%+ confidence BOTH AIs + news catalyst + 5+ signals.
    This is the HIGH CONVICTION filter — fires rarely but powerfully.
    """
    if not trade_claude or not trade_grok:
        return False, "One AI did not propose a trade"

    # ── PRIMARY GATEKEEPER — equity must be $3,000+ ──────────────
    if equity < RULES["collab_unlock_equity"]:
        needed = RULES["collab_unlock_equity"] - equity
        pct    = round(equity / RULES["collab_unlock_equity"] * 100, 1)
        return False, f"LOCKED — need ${needed:.0f} more equity (${equity:.0f}/${RULES['collab_unlock_equity']} = {pct}%)"

    # ── MINIMUM TRADE SIZE — must be able to deploy $1,000 ───────
    if collab_pool < RULES["collab_min_trade_size"]:
        return False, f"Collaborative pool ${collab_pool:.0f} < ${RULES['collab_min_trade_size']} minimum trade size"

    # Must be same symbol
    if trade_claude.get("symbol") != trade_grok.get("symbol"):
        return False, f"Symbol mismatch: Claude={trade_claude.get('symbol')} Grok={trade_grok.get('symbol')}"

    # BONUS: Biggest gainers get automatic collaborative consideration
    # (still need both AIs to agree, but lower confidence threshold)
    gainers      = get_biggest_gainers()
    gainer_syms  = [g["symbol"] for g in gainers if g.get("change", 0) > 3.0]
    is_big_gainer = symbol in gainer_syms
    if is_big_gainer:
        log(f"📈 {symbol} is a biggest gainer today — lowering collab confidence to 88%")

    # Both must hit confidence threshold
    # Biggest gainers get lower threshold (88% vs 95%)
    c_conf     = trade_claude.get("confidence", 0)
    g_conf     = trade_grok.get("confidence", 0)
    conf_floor = 88 if is_big_gainer else RULES["collab_min_confidence"]
    if c_conf < conf_floor:
        return False, f"Claude confidence {c_conf}% < {conf_floor}% required"
    if g_conf < conf_floor:
        return False, f"Grok confidence {g_conf}% < {conf_floor}% required"

    # Must have 5+ combined signals
    c_signals = trade_claude.get("signals", [])
    g_signals = trade_grok.get("signals", [])
    all_signals = list(set(c_signals + g_signals))
    if len(all_signals) < RULES["collab_min_signals"]:
        return False, f"Only {len(all_signals)} unique signals (need {RULES['collab_min_signals']}+)"

    # Must have news catalyst
    symbol = trade_claude.get("symbol", "")
    if RULES["collab_require_news"] and symbol:
        news_lower = news.lower()
        sym_lower  = symbol.lower()
        has_news   = sym_lower in news_lower or any(
            word in news_lower for word in ["earnings", "acquisition", "fed", "rate", "ai", "revenue", "merger"]
        )
        if not has_news:
            return False, f"No news catalyst found for {symbol}"

    # Expected profit check
    expected_profit = trade_claude.get("net_profit_target", 0)
    if expected_profit > 0 and expected_profit < RULES["collab_min_profit_pct"]:
        return False, f"Expected profit {expected_profit*100:.1f}% < {RULES['collab_min_profit_pct']*100:.0f}% minimum"

    return True, f"✅ All gates passed: conf={c_conf}%/{g_conf}% signals={len(all_signals)} news=✅"

def collaborative_session(equity, cash, positions, pos_symbols, open_count,
                          chart_section, news, market_ctx, features, pool):

    pos_details = [
        f"  {p['symbol']}: entry=${float(p['avg_entry_price']):.2f} "
        f"now=${float(p['current_price']):.2f} "
        f"P&L={round(float(p['unrealized_plpc'])*100,2)}% "
        f"owner={'Claude' if p['symbol'] in shared_state['claude_positions'] else 'Grok'}"
        for p in positions
    ]

    can_short    = features.get("can_short", False)
    short_note   = "SHORT SELLING ENABLED" if can_short else f"Short locked (${features.get('until_short',2000):.0f} away)"

    # Get full intelligence for this cycle
    pol_text, pol_trades = get_politician_trades()
    pol_signals  = analyze_politician_signals(pol_trades, chart_section)
    inv_text, inv_holdings = get_top_investor_portfolios()
    gainers      = get_biggest_gainers()
    ipos         = get_recent_ipos()
    smart_money  = analyze_smart_money(pol_signals, inv_holdings, gainers)
    pol_mimick   = pol_signals.get("top_mimick", [])
    gainer_syms  = [g["symbol"] for g in gainers if g.get("in_universe")]
    ipo_syms     = [i["symbol"] for i in ipos[:5]]
    hot_ipos     = [i["symbol"] for i in ipos if abs(i.get("mom_5d", 0)) > 5]
    triple_syms  = smart_money.get("triple_confirmation", [])
    top_collab   = smart_money.get("top_collab", [])

    if triple_syms:
        log(f"🔥 Triple confirmation this cycle: {triple_syms}")
    if gainer_syms:
        log(f"📈 Big gainers for collaborative: {gainer_syms}")
    if ipo_syms:
        log(f"🆕 IPOs in play: {ipo_syms}")

    context = f"""=== AI COLLABORATION TRADING SYSTEM ===
Portfolio: ${equity:.2f} | Cash: ${cash:.2f} | P&L: ${equity-RULES['total_budget']:+.2f}
Growth Reserve: ${pool['reserve']:.2f} (UNTOUCHABLE — never trade this)
Trading Pool: ${pool['trading']:.2f} total
  Claude budget: ${pool['claude']:.2f} ({shared_state['claude_allocation']*100:.1f}%)
  Grok budget:   ${pool['grok']:.2f} ({shared_state['grok_allocation']*100:.1f}%)
Performance: Claude ${shared_state['claude_daily_pnl']:+.2f} today | Grok ${shared_state['grok_daily_pnl']:+.2f} today
Win days: Claude {shared_state['claude_win_days']} | Grok {shared_state['grok_win_days']}
{short_note}
Open positions ({open_count}/{RULES['max_positions']}):
{chr(10).join(pos_details) if pos_details else '  None'}

MARKET: {market_ctx}
NEWS: {news[:300]}

POLITICIAN TRADES (public disclosures — strong signal):
{pol_text[:350]}
Top mimick candidates: {pol_mimick}

BIGGEST GAINERS TODAY >3% (COLLABORATIVE ONLY):
{[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:5]]}

RECENT IPOs (autonomous candidates — high momentum):
{[(i['symbol'], f"{i['days_old']}d old", f"mom={i['mom_5d']}%") for i in ipos[:5]]}
Hot IPOs: {hot_ipos}

TOP INVESTORS (13F): {inv_text[:200]}

SMART MONEY:
🔥 Triple confirmation: {triple_syms}
⭐ Top collaborative: {top_collab}

INDICATORS: {chart_section[:450]}"""

    # Round 1: Both propose independently
    r1_prompt = f"""{context}

IMPORTANT RULES FOR THIS CYCLE:
- AUTONOMOUS trades: technicals + news + politician signals + HOT IPOs (>5% momentum)
- COLLABORATIVE candidates: Biggest gainers (>3%) AND triple confirmation stocks
- IPOs with strong momentum are GOOD autonomous trades — they move fast
- Never suggest a biggest gainer for autonomous — only flag for collaborative
- Politician mimicking: 2+ politicians bought = strong autonomous signal
- Triple confirmation = best collaborative candidate

Propose up to 2 AUTONOMOUS trades from your budget.
Also flag any COLLABORATIVE candidates (biggest gainers or strong politician signals).
JSON: {{"strategy_name":"name","market_thesis":"brief","proposed_trades":[{{"action":"buy/sell/short","symbol":"TICK","notional_usd":15.0,"confidence":85,"direction":"long/short","signals":["s1","s2","s3"],"rationale":"brief","politician_signal":false,"ipo_signal":false}}],"collaborative_candidates":[{{"symbol":"TICK","reason":"biggest gainer OR triple confirmation","confidence":90}}],"bearish_watchlist":["tickers"]}}"""

    log("🔵 Round 1 — Claude proposing...")
    log("🔴 Round 1 — Grok proposing...")
    claude_r1 = ask_with_retry(ask_claude, r1_prompt,
        "You are Claude, disciplined quant trader. ONLY valid JSON under 500 chars.")
    grok_r1   = ask_with_retry(ask_grok,   r1_prompt,
        "You are Grok, momentum trader with Twitter access. ONLY valid JSON under 500 chars.")

    if claude_r1:
        log(f"🔵 Claude: '{claude_r1.get('strategy_name','')}' | {len(claude_r1.get('proposed_trades',[]))} trades")
    if grok_r1:
        log(f"🔴 Grok: '{grok_r1.get('strategy_name','')}' | {len(grok_r1.get('proposed_trades',[]))} trades")

    if not claude_r1 and not grok_r1:
        log("⚠️ Both failed Round 1 — holding")
        return [], False, {}

    # ── AUTONOMOUS TRADES (Round 2 — quick review) ─────────────────
    # Each AI proposes trades for their own fund independently
    c_trades = [(t.get("symbol"),t.get("confidence"),t.get("direction","long"))
                for t in (claude_r1 or {}).get("proposed_trades",[])]
    g_trades = [(t.get("symbol"),t.get("confidence"),t.get("direction","long"))
                for t in (grok_r1 or {}).get("proposed_trades",[])]

    log("🔵 Round 2 — Claude autonomous review...")
    log("🔴 Round 2 — Grok autonomous review...")

    c_review_prompt = f"""Your autonomous trades: {c_trades}. Grok's trades: {g_trades}.
Your budget: ${pool['claude']:.2f}. Confirm your best 1-2 trades (owner=claude).
No overlap with Grok if possible. Min $8. Confidence 80%+.
JSON: {{"refined_trades":[{{"action":"buy/sell/short","symbol":"TICK","notional_usd":15.0,"confidence":85,"direction":"long/short","signals":["s1","s2","s3"],"rationale":"brief","owner":"claude"}}]}}"""

    g_review_prompt = f"""Your autonomous trades: {g_trades}. Claude's trades: {c_trades}.
Your budget: ${pool['grok']:.2f}. Confirm your best 1-2 trades (owner=grok).
Use Twitter sentiment. No overlap with Claude. Min $8. Confidence 80%+.
JSON: {{"refined_trades":[{{"action":"buy/sell/short","symbol":"TICK","notional_usd":15.0,"confidence":85,"direction":"long/short","signals":["s1","s2","s3"],"rationale":"brief","owner":"grok"}}]}}"""

    claude_r2 = ask_with_retry(ask_claude, c_review_prompt,
        "You are Claude confirming your autonomous trades. ONLY valid JSON under 500 chars.")
    grok_r2   = ask_with_retry(ask_grok, g_review_prompt,
        "You are Grok confirming your autonomous trades. ONLY valid JSON under 500 chars.")

    if claude_r2: log(f"🔵 Claude autonomous: {len(claude_r2.get('refined_trades',[]))} trades confirmed")
    if grok_r2:   log(f"🔴 Grok autonomous:   {len(grok_r2.get('refined_trades',[]))} trades confirmed")

    # ── COLLABORATIVE BIG-TICKET CHECK (Round 3) ─────────────────
    # Only fires when BOTH AIs agree at 95%+ with news + 5 signals
    log("🤝 Round 3 — Collaborative big-ticket gate check...")

    collab_trades = []
    collab_budget = pool.get("collaborative", 0)

    # Find highest-confidence matching trades from both AIs
    c_proposals = (claude_r1 or {}).get("proposed_trades", [])
    g_proposals = (grok_r1 or {}).get("proposed_trades", [])

    # Also gather collaborative candidates both AIs flagged
    c_collab_candidates = (claude_r1 or {}).get("collaborative_candidates", [])
    g_collab_candidates = (grok_r1   or {}).get("collaborative_candidates", [])
    all_collab_syms = set(
        [c.get("symbol") for c in c_collab_candidates if c.get("symbol")] +
        [c.get("symbol") for c in g_collab_candidates if c.get("symbol")]
    )
    if all_collab_syms:
        log(f"🤝 Collaborative candidates flagged by AIs: {list(all_collab_syms)}")

    # Add collaborative candidates as synthetic trade proposals for gate check
    for sym in all_collab_syms:
        c_conf_val = next((c.get("confidence",85) for c in c_collab_candidates if c.get("symbol")==sym), 85)
        g_conf_val = next((c.get("confidence",85) for c in g_collab_candidates if c.get("symbol")==sym), 85)
        if c_conf_val >= 85 and g_conf_val >= 85:
            c_proposals.append({"symbol":sym,"action":"buy","confidence":c_conf_val,"signals":["collab_candidate","politician_or_gainer"],"rationale":f"Flagged by Claude as collaborative"})
            g_proposals.append({"symbol":sym,"action":"buy","confidence":g_conf_val,"signals":["collab_candidate","politician_or_gainer"],"rationale":f"Flagged by Grok as collaborative"})

    for c_trade in c_proposals:
        for g_trade in g_proposals:
            if c_trade.get("symbol") == g_trade.get("symbol"):
                worthy, reason = is_collaborative_trade_worthy(
                    c_trade, g_trade, chart_section, news,
                    equity=equity, collab_pool=collab_budget
                )
                if worthy:
                    log(f"🚨 COLLABORATIVE GATE PASSED: {c_trade.get('symbol')} — {reason}")
                    # Size the collaborative trade — minimum $1,000
                    collab_notional = min(
                        collab_budget * RULES["collab_max_trade_pct"],
                        collab_budget - 50  # keep buffer
                    )
                    collab_notional = max(collab_notional, RULES["collab_min_trade_size"])
                    fee_est = estimate_fees(collab_notional)
                    collab_trades.append({
                        "action":       c_trade.get("action", "buy"),
                        "symbol":       c_trade.get("symbol"),
                        "notional_usd": round(collab_notional, 2),
                        "confidence":   min(c_trade.get("confidence",95), g_trade.get("confidence",95)),
                        "owner":        "shared",
                        "rationale":    f"COLLABORATIVE: Claude+Grok both 95%+ | {c_trade.get('rationale','')[:60]}",
                        "fee_estimate": fee_est,
                        "is_collab":    True,
                    })
                    log(f"💥 BIG TICKET: {c_trade.get('symbol')} ${collab_notional:.2f} from collaborative pool!")
                else:
                    log(f"⛔ Collaborative gate FAILED: {c_trade.get('symbol')} — {reason}")

    # Combine autonomous + collaborative trades
    c_ref   = (claude_r2 or {}).get("refined_trades", c_proposals[:2])
    g_ref   = (grok_r2   or {}).get("refined_trades", g_proposals[:2])
    all_trades = c_ref + g_ref + collab_trades

    # Deduplicate — collab takes priority over autonomous for same symbol
    seen_symbols = set()
    final_trades = []
    # Add collab first (priority)
    for t in collab_trades:
        if t.get("symbol") not in seen_symbols:
            final_trades.append(t)
            seen_symbols.add(t.get("symbol"))
    # Add autonomous trades (no overlap with collab)
    for t in c_ref + g_ref:
        if t.get("symbol") not in seen_symbols:
            final_trades.append(t)
            seen_symbols.add(t.get("symbol"))

    all_owned = shared_state["claude_positions"] + shared_state["grok_positions"]
    total_alloc = sum(t.get("notional_usd", 0) for t in final_trades)

    log(f"🤝 Final plan: {len(collab_trades)} collaborative + {len(c_ref)} Claude + {len(g_ref)} Grok trades")
    log(f"💰 Total to deploy: ${total_alloc:.2f} | Cash: ${cash:.2f}")

    for t in final_trades:
        tag = "💥 COLLAB" if t.get("is_collab") else f"[{t.get('owner','?').upper()}]"
        log(f"   {tag} {t.get('action','?').upper()} {t.get('symbol','?')} "
            f"${t.get('notional_usd',0):.2f} conf={t.get('confidence','?')}% "
            f"fee≈${t.get('fee_estimate',0):.3f}")

    # Update bearish watchlist
    for r in [claude_r1, grok_r1]:
        if r:
            for sym in r.get("bearish_watchlist", []):
                if sym not in shared_state["bearish_watchlist"]:
                    shared_state["bearish_watchlist"].append(sym)
    if shared_state["bearish_watchlist"]:
        log(f"📋 Bearish watchlist: {shared_state['bearish_watchlist']}")

    autonomy_unlocked = equity >= 150
    return final_trades, autonomy_unlocked, {"joint_message": f"{len(collab_trades)} collab + {len(c_ref+g_ref)} autonomous trades"}

def execute_trades(final_trades, cash, pos_symbols, open_count, final_plan, features):
    remaining_cash = cash
    new_positions  = open_count
    can_short      = features.get("can_short", False)

    if final_plan.get("autonomy_unlocked"):
        shared_state["autonomy_mode"] = True
        for sym in final_plan.get("claude_autonomous_stocks",[]):
            if sym not in shared_state["claude_positions"]:
                shared_state["claude_positions"].append(sym)
        for sym in final_plan.get("grok_autonomous_stocks",[]):
            if sym not in shared_state["grok_positions"]:
                shared_state["grok_positions"].append(sym)

    for trade in final_trades:
        action   = trade.get("action","hold").lower()
        symbol   = trade.get("symbol")
        notional = float(trade.get("notional_usd", 0))
        conf     = trade.get("confidence", 0)
        owner    = trade.get("owner", "shared")
        fee_est  = trade.get("fee_estimate", estimate_fees(notional))

        if not symbol: continue
        if conf < RULES["min_confidence"]:
            log(f"⚠️ Skip {symbol} — conf {conf}% < {RULES['min_confidence']}%")
            continue

        # Check owner budget
        account = alpaca("GET", "/v2/account")
        equity  = float(account["equity"])
        pool    = get_trading_pool(equity)
        max_for_owner = pool["claude"] if owner == "claude" else pool["grok"] if owner == "grok" else pool["trading"]

        if action == "buy":
            if remaining_cash < 8:
                log(f"⚠️ No buying power (${remaining_cash:.2f}) — skipping {symbol}")
                continue
            if new_positions >= RULES["max_positions"]:
                log(f"⚠️ Max positions — skip {symbol}"); continue
            if symbol in pos_symbols:
                log(f"⚠️ Already own {symbol}"); continue
            notional = min(notional, remaining_cash * 0.90, max_for_owner)
            if notional < 8:
                log(f"⚠️ ${notional:.2f} too small for {symbol}"); continue
            try:
                order = alpaca("POST", "/v2/orders", {
                    "symbol": symbol, "notional": str(round(notional, 2)),
                    "side": "buy", "type": "market", "time_in_force": "day",
                })
                log(f"✅ REAL BUY [{owner}] {symbol} ${notional:.2f} | conf={conf}% | fee≈${fee_est:.3f} | {order['id'][:8]}...")
                remaining_cash -= notional; new_positions += 1
                if owner == "claude" and symbol not in shared_state["claude_positions"]:
                    shared_state["claude_positions"].append(symbol)
                elif owner == "grok" and symbol not in shared_state["grok_positions"]:
                    shared_state["grok_positions"].append(symbol)
                pos_symbols.append(symbol)
            except Exception as e: log(f"❌ Buy {symbol}: {e}")

        elif action == "short":
            if not can_short:
                pct  = features.get("short_progress_pct", 0)
                left = features.get("until_short", 2000)
                log(f"🔒 SHORT {symbol} locked — need ${left:.0f} more ({pct}% to $2k)")
                if symbol not in shared_state["bearish_watchlist"]:
                    shared_state["bearish_watchlist"].append(symbol)
                    log(f"   📋 Added to bearish watchlist: {shared_state['bearish_watchlist']}")
                continue
            if symbol in pos_symbols:
                log(f"⚠️ Already long {symbol} — can't short"); continue
            notional = min(notional, remaining_cash * 0.90, max_for_owner)
            if notional < 8: continue
            try:
                order = alpaca("POST", "/v2/orders", {
                    "symbol": symbol, "notional": str(round(notional, 2)),
                    "side": "sell", "type": "market", "time_in_force": "day",
                })
                log(f"✅ REAL SHORT [{owner}] {symbol} ${notional:.2f} | conf={conf}% | {order['id'][:8]}...")
                remaining_cash -= notional; new_positions += 1
                sym_short = f"{symbol}_SHORT"
                if owner == "claude": shared_state["claude_positions"].append(sym_short)
                elif owner == "grok": shared_state["grok_positions"].append(sym_short)
            except Exception as e: log(f"❌ Short {symbol}: {e}")

        elif action == "sell":
            if symbol not in pos_symbols:
                log(f"⚠️ No position in {symbol}"); continue
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                log(f"✅ REAL SELL [{owner}] {symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                pos_symbols.remove(symbol)
            except Exception as e: log(f"❌ Sell {symbol}: {e}")


def run_low_cash_cycle(positions, pos_symbols, cash, equity, features):
    """
    Low cash mode — fires when buying power is too low to open new positions.

    Strategy:
    1. Both AIs analyze ALL open positions for best exit opportunities
    2. Find the position with best profit to lock in OR weakest to cut
    3. Sell the best candidate to free up cash
    4. Prepare next-buy strategy for when cash is available
    5. Never panic sell — only sell if it makes strategic sense
    """
    log("=" * 50)
    log("💸 LOW CASH CYCLE — Profit-taking + next strategy mode")
    log("=" * 50)

    pos_details = []
    for p in positions:
        pnl_pct  = round(float(p["unrealized_plpc"]) * 100, 2)
        pnl_usd  = round(float(p["unrealized_pl"]), 2)
        owner    = "Claude" if p["symbol"] in shared_state["claude_positions"] else                    "Grok" if p["symbol"] in shared_state["grok_positions"] else "Shared"
        pos_details.append({
            "symbol":   p["symbol"],
            "owner":    owner,
            "pnl_pct":  pnl_pct,
            "pnl_usd":  pnl_usd,
            "qty":      p["qty"],
            "price":    float(p["current_price"]),
            "value":    round(float(p["market_value"]), 2),
        })
        log(f"   [{owner}] {p['symbol']}: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) value=${float(p['market_value']):.2f}")

    # Get quick market check
    try:
        market_ctx    = get_market_context()
        news          = get_news_context()
        chart_section = get_chart_section()
    except Exception as e:
        log(f"⚠️ Data fetch error: {e}")
        market_ctx = news = chart_section = "unavailable"

    # Ask both AIs what to sell and what to prepare for
    low_cash_prompt = f"""LOW CASH SITUATION — Buying power too low to open new trades.
Current cash: ${cash:.2f} (need $8+ to trade)
Total equity: ${equity:.2f}

OPEN POSITIONS (must choose wisely):
{chr(10).join([f"  [{p['owner']}] {p['symbol']}: {p['pnl_pct']:+.2f}% (${p['pnl_usd']:+.2f}) value=${p['value']:.2f}" for p in pos_details])}

MARKET: {market_ctx}
NEWS: {news[:200]}
INDICATORS: {chart_section[:400]}

DECISION FRAMEWORK:
1. Should we sell anything to free up cash? Only if:
   a) Position is at or near take-profit target (>5% gain) — lock it in
   b) Position is showing weakness and likely to drop more
   c) A much better opportunity exists that needs the capital
2. If all positions look good — HOLD, do not panic sell
3. What is the NEXT buy target once cash is available?
4. Which position has the best risk/reward to keep holding?

Respond ONLY with JSON:
{{"sell_recommendation": "symbol or none","sell_reason": "brief","hold_recommendation": ["symbols to keep"],"next_buy_target": "symbol","next_buy_reason": "brief","action": "sell/hold","urgency": "high/medium/low"}}"""

    claude_decision = None
    grok_decision   = None

    try:
        claude_decision = ask_with_retry(ask_claude, low_cash_prompt,
            "You are Claude managing low cash situation. ONLY valid JSON under 500 chars.")
        if claude_decision:
            log(f"🔵 Claude low-cash: action={claude_decision.get('action')} sell={claude_decision.get('sell_recommendation')} next={claude_decision.get('next_buy_target')}")
    except Exception as e:
        log(f"❌ Claude low-cash: {e}")

    try:
        grok_decision = ask_with_retry(ask_grok, low_cash_prompt,
            "You are Grok managing low cash situation. ONLY valid JSON under 500 chars.")
        if grok_decision:
            log(f"🔴 Grok low-cash: action={grok_decision.get('action')} sell={grok_decision.get('sell_recommendation')} next={grok_decision.get('next_buy_target')}")
    except Exception as e:
        log(f"❌ Grok low-cash: {e}")

    # ── DECISION LOGIC ──────────────────────────────────────
    # Only sell if BOTH AIs agree on same symbol
    c_sell = (claude_decision or {}).get("sell_recommendation", "none").upper()
    g_sell = (grok_decision   or {}).get("sell_recommendation", "none").upper()
    c_action = (claude_decision or {}).get("action", "hold").lower()
    g_action = (grok_decision   or {}).get("action", "hold").lower()

    # Check if any position hit take-profit or stop-loss automatically
    sold_something = False
    for p in pos_details:
        if p["pnl_pct"] >= RULES["take_profit_pct"] * 100:
            log(f"🎯 [{p['owner']}] Auto take-profit: {p['symbol']} at +{p['pnl_pct']:.1f}%")
            try:
                alpaca("DELETE", f"/v2/positions/{p['symbol']}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != p["symbol"]]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != p["symbol"]]
                log(f"✅ SOLD {p['symbol']} — profit locked")
                sold_something = True
            except Exception as e:
                log(f"❌ Sell {p['symbol']}: {e}")

        elif p["pnl_pct"] <= -RULES["stop_loss_pct"] * 100:
            log(f"🛑 [{p['owner']}] Auto stop-loss: {p['symbol']} at {p['pnl_pct']:.1f}%")
            try:
                alpaca("DELETE", f"/v2/positions/{p['symbol']}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != p["symbol"]]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != p["symbol"]]
                log(f"✅ SOLD {p['symbol']} — loss cut")
                sold_something = True
            except Exception as e:
                log(f"❌ Sell {p['symbol']}: {e}")

    # If both AIs agree to sell same symbol AND it's not already auto-sold
    if c_sell == g_sell and c_sell != "NONE" and c_sell in pos_symbols and not sold_something:
        log(f"🤝 Both AIs agree: SELL {c_sell} to free up cash")
        log(f"   Claude reason: {(claude_decision or {}).get('sell_reason','')}")
        log(f"   Grok reason:   {(grok_decision   or {}).get('sell_reason','')}")
        try:
            alpaca("DELETE", f"/v2/positions/{c_sell}")
            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != c_sell]
            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != c_sell]
            log(f"✅ SOLD {c_sell} — cash freed up for better opportunity")
            sold_something = True
        except Exception as e:
            log(f"❌ Sell {c_sell}: {e}")

    elif c_action == "hold" and g_action == "hold":
        log(f"🤝 Both AIs agree: HOLD all positions — not worth selling yet")
        log(f"   Best position: {max(pos_details, key=lambda x: x['pnl_pct'])['symbol'] if pos_details else 'none'}")

    elif c_sell != g_sell and c_sell != "NONE" and g_sell != "NONE":
        log(f"⚠️ AIs disagree on what to sell (Claude={c_sell} Grok={g_sell}) — HOLDING")
        log(f"   Will wait for clearer signal or auto stop-loss/take-profit")

    # ── NEXT STRATEGY LOG ───────────────────────────────────
    c_next = (claude_decision or {}).get("next_buy_target", "")
    g_next = (grok_decision   or {}).get("next_buy_target", "")

    log(f"📋 NEXT BUY TARGETS (ready when cash available):")
    if c_next: log(f"   🔵 Claude: {c_next} — {(claude_decision or {}).get('next_buy_reason','')[:80]}")
    if g_next: log(f"   🔴 Grok:   {g_next} — {(grok_decision   or {}).get('next_buy_reason','')[:80]}")

    if c_next == g_next and c_next:
        log(f"   🤝 AGREED: Both targeting {c_next} — will buy first chance!")
        shared_state["next_buy_target"] = c_next
    elif c_next or g_next:
        shared_state["next_buy_target"] = c_next or g_next

    log("=" * 50)
    log(f"💸 Low cash cycle complete | Cash: ${cash:.2f} | Positions: {len(positions)}")
    log("=" * 50)

def run_cycle():
    log("── 🤝 Collaboration Cycle ──")
    if not is_market_open():
        log("Market closed."); return

    account   = alpaca("GET", "/v2/account")
    equity    = float(account["equity"])
    cash      = float(account["cash"])
    features  = check_account_features(account, equity)
    pool      = get_trading_pool(equity)

    log(f"💰 REAL Equity: ${equity:.2f} | Cash: ${cash:.2f} | P&L: ${equity-RULES['total_budget']:+.2f}")

    # Check for tier upgrades
    tier_upgraded, tier_data = check_autonomy_tier(equity)
    autonomy = get_autonomy_status(equity)
    if not shared_state["autonomy_mode"]:
        log(f"🎯 Autonomy progress: {autonomy['progress_pct']}% — need ${autonomy['needed']:.2f} for Tier 1 (${autonomy['next_fund']} each AI)")
    else:
        log(f"🔓 Tier {shared_state['autonomy_tier']}: Claude=${shared_state['claude_auto_fund']:.2f} | Grok=${shared_state['grok_auto_fund']:.2f}")
        if autonomy.get("needed", 0) > 0:
            log(f"🎯 Next tier: ${autonomy['needed']:.2f} away — {autonomy.get('next_description','')}")

    if pool["autonomy_active"]:
        log(f"💼 Tier {pool['tier']}: Claude=${pool['claude']:.2f}(auto) | Grok=${pool['grok']:.2f}(auto) | Collab=${pool['collaborative']:.2f} | Reserve=${pool['reserve']:.2f}")
    else:
        log(f"💼 Pool: ${pool['trading']:.2f} collaborative | Reserve=${pool['reserve']:.2f} (safe)")

    # Collaborative big-ticket status
    collab_needed = max(0, RULES["collab_unlock_equity"] - equity)
    if collab_needed > 0:
        collab_pct = round(equity / RULES["collab_unlock_equity"] * 100, 1)
        log(f"🔒 Collaborative big-ticket: LOCKED — need ${collab_needed:.0f} more (${equity:.0f}/${RULES['collab_unlock_equity']} = {collab_pct}% | min trade ${RULES['collab_min_trade_size']:,})")
    else:
        log(f"💥 Collaborative big-ticket: UNLOCKED — pool=${pool.get('collaborative',0):.2f} | min trade ${RULES['collab_min_trade_size']:,}")

    loss_pct = (RULES["total_budget"] - equity) / RULES["total_budget"]
    if loss_pct >= RULES["daily_loss_limit_pct"]:
        log(f"🛑 Daily loss limit {loss_pct*100:.1f}% — STOPPING today."); return

    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)
    log(f"Positions ({open_count}): {pos_symbols or 'none'}")

    track_pnl(positions)
    log(f"📊 Today: Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}")

    check_exit_conditions(positions)
    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]
    open_count  = len(positions)

    # ── CASH MODE DETECTION ──────────────────────────────────
    min_trade    = 8.0
    has_cash     = cash >= min_trade
    has_positions = open_count > 0

    if not has_cash and not has_positions:
        log("⚠️ No cash and no positions — fully flat. Waiting for next cycle.")
        return

    if not has_cash and has_positions:
        log(f"💸 LOW CASH MODE (${cash:.2f}) — Switching to profit-taking focus")
        log(f"   Positions open: {pos_symbols}")
        log(f"   Strategy: find best exit on current positions, prepare for reentry")
        run_low_cash_cycle(positions, pos_symbols, cash, equity, features)
        return

    if has_cash and cash < min_trade * 2:
        log(f"⚠️ VERY LOW CASH (${cash:.2f}) — Only executing highest confidence trades")

    log("📡 Fetching news + market...")
    news       = get_news_context()
    market_ctx = get_market_context()
    log("📊 Computing indicators...")
    chart_section = get_chart_section()

    final_trades, autonomy_unlocked, final_plan = collaborative_session(
        equity, cash, positions, pos_symbols, open_count,
        chart_section, news, market_ctx, features, pool
    )

    if not final_trades:
        log("⏳ No trades agreed — holding.")
    else:
        execute_trades(final_trades, cash, pos_symbols, open_count, final_plan, features)

    shared_state["last_sync"] = datetime.now().isoformat()
    log("── Cycle complete ──\n")

def run_premarket():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins_to_open = max(0, 570 - (now_et.hour * 60 + now_et.minute))
    log(f"📊 PRE-MARKET ({mins_to_open}min to open) — Research phase...")

    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])
    pool    = get_trading_pool(equity)

    intel        = get_full_market_intelligence()
    chart        = intel["chart_section"]
    news         = intel["news"]
    market       = intel["market_ctx"]
    pol_text     = intel["pol_text"]
    pol_signals  = intel["pol_signals"]
    gainers      = intel["gainers"]
    inv_text     = intel["inv_text"]
    smart_money  = intel["smart_money"]

    ipos         = intel.get("ipos", [])
    pol_mimick   = pol_signals.get("top_mimick", [])
    pol_buys     = pol_signals.get("universe_buys", [])
    gainer_syms  = [g["symbol"] for g in gainers if g.get("in_universe")]
    triple_syms  = smart_money.get("triple_confirmation", [])
    top_collab   = smart_money.get("top_collab", [])
    ipo_syms     = [i["symbol"] for i in ipos[:5]]
    hot_ipos     = [i for i in ipos if abs(i.get("mom_5d", 0)) > 5]

    research_prompt = f"""Pre-market research. Market opens in {mins_to_open} min.
Budget: Claude=${pool['claude']:.2f} | Grok=${pool['grok']:.2f} | Reserve=${pool['reserve']:.2f} (untouchable)
Performance: Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}

MARKET: {market}
NEWS (24h): {news[:200]}

POLITICIAN TRADES: {pol_text[:200]}
Top mimick: {pol_mimick}

TOP INVESTORS (13F): {inv_text[:200]}

RECENT IPOs (30-180 days old — high momentum potential):
{[(i['symbol'], f"{i['days_old']}d old", f"mom={i['mom_5d']}%") for i in ipos[:5]]}
Hot IPOs (>5% momentum): {[i['symbol'] for i in hot_ipos]}

SMART MONEY:
🔥 Triple confirmation: {triple_syms}
⭐ Top collaborative: {top_collab}
📈 Biggest gainers (>3%): {[(g['symbol'], f'+{g["change"]:.1f}%') for g in gainers[:5]]}

INDICATORS: {chart[:350]}

Research tasks:
1. Triple confirmation = highest priority
2. Hot IPOs with strong momentum — good autonomous candidates
3. Politician + investor combos aligned with technicals
4. Biggest gainers → collaborative only
Plain text 150 words."""

    try:
        c_research = ask_claude(research_prompt,
            "You are Claude doing pre-market research. Plain text response.", max_tokens=400)
        log(f"🔵 Claude research:\n{c_research[:400]}")
    except Exception as e:
        log(f"❌ Claude research: {e}")

    try:
        g_research = ask_grok(research_prompt,
            "You are Grok doing pre-market research with Twitter access. Plain text.", max_tokens=400)
        log(f"🔴 Grok research:\n{g_research[:400]}")
    except Exception as e:
        log(f"❌ Grok research: {e}")

def run_afterhours():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins_since_close = (now_et.hour * 60 + now_et.minute) - 960
    log(f"📈 AFTER-HOURS ({mins_since_close}min after close) — Review + planning...")

    try:
        account   = alpaca("GET", "/v2/account")
        positions = alpaca("GET", "/v2/positions")
        equity    = float(account["equity"])
        pnl       = equity - RULES["total_budget"]
        pool      = get_trading_pool(equity)

        log(f"💰 Day End: ${equity:.2f} | P&L: ${pnl:+.2f}")
        log(f"💼 Reserve: ${pool['reserve']:.2f} (protected) | Trading pool: ${pool['trading']:.2f}")
        log(f"📊 Today: Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}")
        log(f"🏆 Total: Claude ${shared_state['claude_total_pnl']:+.2f} | Grok ${shared_state['grok_total_pnl']:+.2f}")
        log(f"🏅 Win days: Claude {shared_state['claude_win_days']} | Grok {shared_state['grok_win_days']}")

        for p in positions:
            pnl_pct = round(float(p["unrealized_plpc"])*100,2)
            owner = "Claude" if p["symbol"] in shared_state["claude_positions"] else "Grok"
            log(f"   [{owner}] {p['symbol']}: {pnl_pct:+.2f}%")

        if not positions:
            log("✅ Fully in cash overnight")

        # Daily rebalance
        rebalance_allocations(daily=True)

        # Weekly rebalance check
        now_et = datetime.now(ZoneInfo("America/New_York"))
        week   = now_et.isocalendar()[1]
        if shared_state.get("last_rebalance_week") != week and now_et.weekday() == 4:
            log("📅 WEEKLY REBALANCE!")
            rebalance_allocations(daily=False)

        # Both AIs plan tomorrow
        news  = get_news_context()
        chart = get_chart_section()

        # Fetch end of day intelligence
        # ── PHASE 1: Specialized After-Hours Research ──────
        log("=" * 50)
        log("📋 AFTER-HOURS PHASE 1: Specialized domain review")
        log("=" * 50)
        log("🔵 Claude reviewing: Politician filings + Investor moves")
        log("🔴 Grok reviewing:   After-hours IPO moves + Tomorrow sentiment")

        intel_ah    = get_full_market_intelligence()
        pol_text_ah = intel_ah["pol_text"]
        inv_text_ah = intel_ah["inv_text"]
        ipos_ah     = intel_ah.get("ipos", [])
        gainers_ah  = intel_ah["gainers"]
        smart_ah    = intel_ah["smart_money"]

        # Claude reviews smart money for tomorrow
        claude_ah_prompt = f"""AFTER-HOURS — You are CLAUDE reviewing smart money signals for TOMORROW.

TODAY'S PERFORMANCE:
P&L: ${pnl:+.2f} | Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}
Positions held: {[p['symbol'] for p in positions]}

NEW POLITICIAN FILINGS (just disclosed):
{pol_text_ah[:400]}

TOP INVESTOR MOVES (any new 13F updates):
{inv_text_ah[:300]}

TOMORROW'S SMART MONEY SETUP:
Triple confirmation: {smart_ah.get("triple_confirmation", [])}
Top collaborative: {smart_ah.get("top_collab", [])}

ANALYSIS TASKS:
1. Any NEW politician filings today that change tomorrow's outlook?
2. Are politicians buying into weakness? (contrarian signal)
3. Which smart money positions should we mirror tomorrow?
4. Any politician SELLS to watch as warning signals?
5. Recommend: hold overnight positions or go to cash?
Plain text 180 words."""

        # Grok reviews momentum for tomorrow
        grok_ah_prompt = f"""AFTER-HOURS — You are GROK reviewing momentum signals for TOMORROW.

TODAY'S PERFORMANCE:
P&L: ${pnl:+.2f} | Claude ${shared_state['claude_daily_pnl']:+.2f} | Grok ${shared_state['grok_daily_pnl']:+.2f}

AFTER-HOURS IPO ACTIVITY:
{[(i["symbol"], f"mom={i['mom_5d']}%", f"price=${i['price']}") for i in ipos_ah[:6]]}

AFTER-HOURS MOVERS:
{[(g["symbol"], f'+{g["change"]:.1f}%') for g in gainers_ah[:5]]}

NEWS: {news[:300]}

ANALYSIS TASKS:
1. Which IPOs are showing after-hours strength? (pre-market gap up likely)
2. Twitter/X sentiment for tomorrow — fear or greed?
3. Any earnings surprises affecting our universe?
4. Which of today's losers might bounce tomorrow?
5. Top 3 momentum plays for tomorrow's open
6. Bearish watchlist update — any new short candidates?
Plain text 180 words."""

        claude_ah = ""
        grok_ah   = ""

        try:
            claude_ah = ask_claude(claude_ah_prompt,
                "You are Claude doing after-hours smart money review. Plain text.", max_tokens=500)
            log(f"🔵 Claude after-hours review:\n{claude_ah[:500]}")
        except Exception as e:
            log(f"❌ Claude after-hours: {e}")

        try:
            grok_ah = ask_grok(grok_ah_prompt,
                "You are Grok doing after-hours momentum review. Plain text.", max_tokens=500)
            log(f"🔴 Grok after-hours review:\n{grok_ah[:500]}")
        except Exception as e:
            log(f"❌ Grok after-hours: {e}")

        # ── PHASE 2: Joint Tomorrow Plan ───────────────────
        log("=" * 50)
        log("📋 AFTER-HOURS PHASE 2: Joint plan for tomorrow")
        log("=" * 50)

        tomorrow_prompt = f"""Create TOMORROW'S JOINT AGREED PLAN.

Claude's review (smart money): {claude_ah[:350] if claude_ah else "unavailable"}
Grok's review (momentum):      {grok_ah[:350] if grok_ah else "unavailable"}

Today's results: ${pnl:+.2f} total P&L
New allocations: Claude {shared_state['claude_allocation']*100:.1f}% | Grok {shared_state['grok_allocation']*100:.1f}%
Bearish watchlist: {shared_state['bearish_watchlist']}

JOINT PLAN FOR TOMORROW:
1. OVERNIGHT DECISION: Hold or sell each open position (specific reasoning)
2. PRE-MARKET FOCUS: Top 3 stocks to watch at open
3. CLAUDE'S STRATEGY tomorrow (smart money + technical)
4. GROK'S STRATEGY tomorrow (momentum + IPO + sentiment)
5. COLLABORATIVE TARGET: Best big-ticket candidate if both agree
6. RISK LEVEL for tomorrow and position sizing guidance
7. LESSONS from today: what worked, what didn't

Both AIs agree on this plan. Plain text 200 words."""

        try:
            tomorrow_plan = ask_claude(tomorrow_prompt,
                "You are creating tomorrow's agreed trading plan. Plain text.", max_tokens=500)
            log(f"\n{'='*50}")
            log(f"✅ TOMORROW'S JOINT PLAN (AGREED):")
            log(f"{'='*50}")
            log(f"{tomorrow_plan[:600]}")
            log(f"{'='*50}\n")
            shared_state["tomorrows_plan"] = tomorrow_plan
        except Exception as e:
            log(f"❌ Tomorrow plan: {e}")

    except Exception as e:
        log(f"❌ After-hours error: {e}")

def trading_loop():
    log(f"🚀 COLLABORATIVE AI Trading System v2.0")
    log(f"💰 Budget: ${RULES['total_budget']} | Reserve: {RULES['growth_reserve_pct']*100:.0f}% untouchable")
    log(f"⚖️ Start: 50/50 split → performance-based rebalance daily + weekly")
    log(f"🏆 Autonomy Tiers:")
    for tier in RULES["autonomy_tiers"]:
        log(f"   ${tier['equity']} → {tier['description']}")
    log(f"🔒 Short selling unlocks at $2,000")
    log(f"💥 Collaborative big-ticket unlocks at $3,000 (min trade ${RULES['collab_min_trade_size']:,})")
    log(f"🆕 IPO detection: active (30-180 day old stocks, >500k volume)")
    log(f"🛡️ Stop={RULES['stop_loss_pct']*100}% | TP={RULES['take_profit_pct']*100}% | Daily limit={RULES['daily_loss_limit_pct']*100}%")

    if not all([ALPACA_KEY, ALPACA_SECRET, ANTHROPIC_KEY, GROK_KEY]):
        log("❌ Missing env vars!"); return

    # Initialize day/week tracking
    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])
    shared_state["day_start_equity"]  = equity
    shared_state["week_start_equity"] = equity
    shared_state["last_equity"]       = equity

    last_premarket  = None
    last_afterhours = None

    while True:
        try:
            mode, interval = get_market_mode()
            now_et = datetime.now(ZoneInfo("America/New_York"))
            today  = now_et.date()

            if mode == "sleep":
                next_check = (now_et + timedelta(minutes=interval)).strftime("%H:%M ET")
                log(f"😴 Sleeping {interval} min. Next: {next_check}")

            elif mode == "premarket":
                run_premarket()

            elif mode in ("opening", "prime", "power_hour"):
                labels = {"opening":"🔔 OPENING","prime":"🚀 PRIME","power_hour":"⚡ POWER HOUR"}
                log(f"{labels[mode]} — Collaboration cycle")
                run_cycle()

            elif mode == "afterhours":
                run_afterhours()

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
