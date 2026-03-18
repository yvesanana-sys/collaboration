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
    # ── Exit Strategy Options ──
    # Strategy A: Fixed take-profit (fast, predictable)
    "exit_A_take_profit":      0.07,   # 7% fixed take-profit
    "exit_A_stop_loss":        0.04,   # 4% hard stop
    "exit_A_time_stop_days":   None,   # No time stop
    # Strategy B: Trailing stop (lets winners run)
    "exit_B_trail_default":    0.05,   # 5% trail below peak
    "exit_B_trail_volatile":   0.08,   # 8% trail for TSLA/MSTR/COIN
    "exit_B_trail_stable":     0.03,   # 3% trail for AAPL/MSFT
    "exit_B_trail_activates":  0.03,   # Trailing activates at +3% profit
    "exit_B_stop_loss":        0.04,   # Same hard stop
    "exit_B_time_stop_days":   3,      # Sell if no +3% after 3 days
    # Volatile stocks (wider trail needed)
    "volatile_stocks": ["TSLA","MSTR","COIN","RKLB","SOFI","AMD"],
    # Stable stocks (tighter trail)
    "stable_stocks":   ["AAPL","MSFT","GOOGL","AMZN","META"],
    # ── Cash threshold tiers (BASE — scales dynamically with equity) ──
    "cash_sleep_threshold":    8,    # Absolute minimum — never changes
    "cash_watch_base_pct":     0.04, # Watch mode = 4% of equity (min $20)
    "cash_active_base_pct":    0.05, # Full collab = 5% of equity (min $20)
    # ── Dynamic threshold scaling by equity ──
    # As account grows, watch/active thresholds scale proportionally
    # $55   account → watch=$20,  active=$20  (floor)
    # $500  account → watch=$25,  active=$30
    # $1000 account → watch=$50,  active=$60
    # $5000 account → watch=$200, active=$250
    "threshold_floor":         20,   # Minimum watch threshold ever
    "threshold_equity_pct":    0.05, # 5% of equity = watch threshold
    "threshold_active_mult":   1.2,  # Active = watch × 1.2
    # ── AI failover ──
    "failover_max_retries":    3,
    "autopilot_rsi_buy":       35,
    "autopilot_rsi_sell":      70,
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
    "spy_trend":            "neutral",
    # ── AI Sleep/Wake System ──
    "ai_sleeping":          False,   # True = both AIs asleep, bot runs autonomous
    "sleep_reason":         None,    # Why they went to sleep
    "wake_reason":          None,    # What woke them up
    "last_sleep_time":      None,
    "last_wake_time":       None,
    "stops_fired_today":    0,       # Count of stop-losses fired while sleeping
    "sleeping_strategies":  {},      # Full exit strategies stored before sleep
    "ai_notes":             {},      # AI notes per position for bot to follow
    # ── Trading Brief System ──────────────────────────────────
    "trading_brief": {
        "account": {
            "market_bias":    "neutral",
            "risk_level":     "medium",
            "max_new_trades": 2,
            "avoid_sectors":  [],
            "spy_rule":       "no_buy_bear",
            "daily_target":   0.02,
            "stop_day_if":    -0.05,
            "brief_date":     None,
            "brief_notes":    "",
        },
        "positions":   {},   # Per-position strategy + notes
        "watchlist":   [],   # Buy these when cash available
        "collab_targets": [], # Collaborative big-ticket targets
        "tomorrows_brief": None,  # Draft for tomorrow
    },
    # Exit strategy tracking per position
    # Format: {"TICKER": {"strategy":"A/B","peak_price":0,"entry_date":"","entry_price":0}}
    "position_exits":       {},
    # AI health status
    "claude_healthy":       True,
    "grok_healthy":         True,
    "claude_fail_count":    0,
    "grok_fail_count":      0,
    "claude_fail_reason":   None,   # credits_exhausted/network_error/auth_error
    "grok_fail_reason":     None,
    "claude_credits_ok":    True,   # False = needs manual top-up
    "grok_credits_ok":      True,
    "last_claude_fail":     None,
    "last_grok_fail":       None,
    "failover_mode":        None,   # None/claude_only/grok_only/autopilot
    "watch_mode_active":    False,
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
            "claude_healthy":     shared_state["claude_healthy"],
            "claude_credits_ok":  shared_state["claude_credits_ok"],
            "claude_fail_reason": shared_state["claude_fail_reason"],
            "grok_healthy":       shared_state["grok_healthy"],
            "grok_credits_ok":    shared_state["grok_credits_ok"],
            "grok_fail_reason":   shared_state["grok_fail_reason"],
            "failover_mode":      shared_state["failover_mode"],
            "watch_mode_active":  shared_state["watch_mode_active"],
            "ai_sleeping":        shared_state["ai_sleeping"],
            "sleep_reason":       shared_state["sleep_reason"],
            "wake_reason":        shared_state["wake_reason"],
            "stops_fired_today":  shared_state["stops_fired_today"],
            "cash_thresholds":    get_cash_thresholds(equity),
            "can_short":          features["can_short"],
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
    Get recent politician stock trades using Grok's real-time web access.
    Grok can search the web for latest congressional trading disclosures.
    This bypasses Railway network restrictions on external APIs.
    """
    try:
        universe_str = ", ".join(RULES["universe"])
        prompt = f"""Search for the most recent US politician/congress member stock trades 
filed in the last 30 days. Focus on these stocks if mentioned: {universe_str}
Also include any trades in NVDA, AAPL, MSFT, AMZN, TSLA, META, GOOGL.

Return ONLY a JSON object:
{{"trades": [
  {{"politician": "Name", "party": "D/R", "ticker": "SYMBOL", "action": "buy/sell", "size": "$1k-$15k", "filed": "YYYY-MM-DD"}}
], "summary": "brief overview of trends"}}
If no data found return {{"trades": [], "summary": "no recent data"}}"""

        raw = ask_grok(prompt,
            "You are a financial research assistant with web access. Search for recent US congressional stock trades. Return ONLY valid JSON.")
        result = parse_json(raw)

        if result and result.get("trades"):
            trades = result["trades"]
            lines  = [
                f"  [{t.get('party','?')}] {t.get('politician','?')}: "
                f"{t.get('action','?').upper()} {t.get('ticker','?')} "
                f"{t.get('size','')} ({t.get('filed','')})"
                for t in trades[:12]
            ]
            summary = result.get("summary", "")
            if summary:
                log(f"🏛️ Politician trade summary: {summary[:100]}")
            return "\n".join(lines), trades

        return "  No recent politician trades found via Grok", []

    except Exception as e:
        log(f"⚠️ Politician trades via Grok failed: {e}")
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
    Track portfolios of top investors using Grok's real-time web/knowledge access.
    Uses both Grok's training knowledge and web search for latest 13F data.
    Bypasses Railway network restrictions on SEC EDGAR.
    """
    log("💼 Fetching top investor portfolios via Grok...")
    universe_set = set(RULES["universe"])

    try:
        universe_str = ", ".join(RULES["universe"])
        prompt = f"""What are the current major stock holdings of these top investors based on their latest 13F filings and recent news:
Cathie Wood (ARK Invest), Warren Buffett (Berkshire), Michael Burry, George Soros, Ray Dalio (Bridgewater), Bill Ackman (Pershing Square), Stanley Druckenmiller

Focus specifically on these stocks if they hold them: {universe_str}
Also note any recent buys or sells in the last quarter.

Return ONLY a JSON object:
{{"holdings": [
  {{"investor": "Name", "symbol": "TICKER", "position": "large/medium/small", "recent_change": "new buy/increased/decreased/sold/held", "notes": "brief"}}
], "key_insight": "most important trend across all investors"}}"""

        raw = ask_grok(prompt,
            "You are a financial analyst with knowledge of institutional investor 13F filings. Return ONLY valid JSON.")
        result = parse_json(raw)

        if result and result.get("holdings"):
            holdings_list = result["holdings"]
            all_holdings  = {}

            for h in holdings_list:
                sym = h.get("symbol","").upper()
                if sym in universe_set:
                    if sym not in all_holdings:
                        all_holdings[sym] = []
                    all_holdings[sym].append({
                        "investor": h.get("investor",""),
                        "value":    0,
                        "filed":    "latest 13F",
                        "change":   h.get("recent_change","held"),
                        "notes":    h.get("notes",""),
                    })

            if all_holdings:
                lines = []
                for sym, holders in all_holdings.items():
                    investors = [f"{h['investor'].split('(')[0].strip()} ({h['change']})"
                                 for h in holders]
                    lines.append(f"  {sym}: {', '.join(investors)}")

                insight = result.get("key_insight","")
                if insight:
                    log(f"💼 Key investor insight: {insight[:120]}")

                log(f"💼 Universe stocks held by top investors: {list(all_holdings.keys())}")
                return "\n".join(lines), all_holdings

    except Exception as e:
        log(f"⚠️ Investor portfolios via Grok failed: {e}")

    # Fallback: hardcoded known major holdings
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

def get_trail_pct(symbol):
    """Get volatility-adjusted trailing stop percentage for a stock"""
    if symbol in RULES["volatile_stocks"]:
        return RULES["exit_B_trail_volatile"]   # 8% for volatile
    elif symbol in RULES["stable_stocks"]:
        return RULES["exit_B_trail_stable"]     # 3% for stable
    return RULES["exit_B_trail_default"]        # 5% default

def assign_exit_strategy(symbol, strategy, entry_price, confidence=80, rationale=""):
    """
    Assign exit strategy to a position when it's opened.
    Strategy A = fixed take-profit (fast trades, news-driven)
    Strategy B = trailing stop (momentum/trend plays, let winners run)
    """
    trail_pct = get_trail_pct(symbol)
    shared_state["position_exits"][symbol] = {
        "strategy":    strategy,
        "entry_price": entry_price,
        "peak_price":  entry_price,   # Tracks highest price seen
        "entry_date":  datetime.now().strftime("%Y-%m-%d"),
        "trail_pct":   trail_pct,
        "confidence":  confidence,
        "rationale":   rationale[:100],
    }
    if strategy == "A":
        log(f"📋 {symbol} exit strategy: A (fixed {RULES['exit_A_take_profit']*100:.0f}% TP) — {rationale[:60]}")
    else:
        log(f"📋 {symbol} exit strategy: B (trailing {trail_pct*100:.0f}% stop, {RULES['exit_B_time_stop_days']}d time) — {rationale[:60]}")

def decide_exit_strategy_solo(symbol, trade_data, bars, ind):
    """
    Single AI decides exit strategy autonomously.
    Called when one AI is making an autonomous trade.
    Claude uses technical signals, Grok uses momentum signals.
    """
    # Heuristic rules (fast, no API call needed for autonomous trades)
    signals_str = " ".join(trade_data.get("signals", []))

    # Strategy B signals (trailing — let it run)
    b_signals = [
        ind and ind.get("mom_5d", 0) and abs(ind["mom_5d"]) > 3,  # Strong momentum
        "ipo" in signals_str.lower(),                               # IPO momentum
        "momentum" in signals_str.lower(),                         # Momentum play
        "breakout" in signals_str.lower(),                         # Breakout
        trade_data.get("ipo_signal", False),                       # IPO flag
        ind and ind.get("vol_ratio", 1) > 1.5,                    # High volume
    ]

    # Strategy A signals (fixed — take profit quickly)
    a_signals = [
        "news" in signals_str.lower(),          # News-driven (can reverse fast)
        "politician" in signals_str.lower(),    # Politician signal
        "earnings" in signals_str.lower(),      # Earnings play (take quick profit)
        trade_data.get("politician_signal", False),
    ]

    b_count = sum(1 for s in b_signals if s)
    a_count = sum(1 for s in a_signals if s)

    if b_count >= 2:
        return "B", f"momentum signals ({b_count} B-signals) → let it run"
    elif a_count >= 2:
        return "A", f"news/event driven ({a_count} A-signals) → take quick profit"
    else:
        # Default: high confidence = B (trust the signal), low = A (take what you can)
        conf = trade_data.get("confidence", 80)
        if conf >= 88:
            return "B", f"high confidence {conf}% → trailing stop"
        else:
            return "A", f"moderate confidence {conf}% → fixed take-profit"

def decide_exit_strategy_collaborative(symbol, claude_view, grok_view):
    """
    Both AIs must AGREE on exit strategy for collaborative trades.
    If they disagree → default to safer Strategy A.
    """
    c_strategy = claude_view.get("exit_strategy","A").upper()
    g_strategy = grok_view.get("exit_strategy","A").upper()
    c_reason   = claude_view.get("exit_rationale","")
    g_reason   = grok_view.get("exit_rationale","")

    if c_strategy == g_strategy:
        log(f"🤝 {symbol} exit strategy AGREED: {c_strategy}")
        log(f"   Claude: {c_reason[:60]}")
        log(f"   Grok:   {g_reason[:60]}")
        return c_strategy, f"both agreed: {c_reason[:40]}"
    else:
        log(f"⚠️ {symbol} exit strategy DISAGREED: Claude={c_strategy} Grok={g_strategy}")
        log(f"   Defaulting to Strategy A (safer) — no consensus")
        return "A", f"no consensus (C={c_strategy} G={g_strategy}) → fixed TP"

def get_spy_trend():
    """
    Check SPY trend vs 50-day SMA.
    Only buy when market is in uptrend — dramatically improves win rate.
    Returns: "bull" / "bear" / "neutral"
    """
    try:
        bars = get_bars("SPY", days=60)
        if len(bars) >= 50:
            closes  = [b["c"] for b in bars]
            sma50   = sum(closes[-50:]) / 50
            current = closes[-1]
            change  = round((current - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else 0
            if current > sma50 * 1.01:
                return "bull", current, sma50, change
            elif current < sma50 * 0.99:
                return "bear", current, sma50, change
            else:
                return "neutral", current, sma50, change
    except Exception as e:
        log(f"⚠️ SPY trend check failed: {e}")
    return "neutral", 0, 0, 0

def get_dynamic_take_profit(notional, pnl_pct, position_value):
    """
    Dynamic take-profit that lets winners run further on larger positions.
    Small position:  take at 7% (original)
    Medium position: take at 10%
    Large position:  take at 15%
    This lets big positions compound without cutting them too early.
    """
    if position_value >= 100:
        return 0.15   # 15% for large positions
    elif position_value >= 50:
        return 0.10   # 10% for medium positions
    else:
        return RULES["take_profit_pct"]  # 7% default for small

def smart_sell(symbol, reason, pos):
    """Execute a smart limit sell, fall back to market"""
    try:
        snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
        headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        try:
            snap_res = requests.get(snap_url, headers=headers, timeout=5)
            if snap_res.ok:
                quote     = snap_res.json().get("quote", {})
                bid_price = float(quote.get("bp", 0))
                ask_price = float(quote.get("ap", 0))
                if bid_price > 0 and ask_price > 0:
                    sell_price = round((bid_price + ask_price) / 2, 2)
                    qty        = pos["qty"]
                    order = alpaca("POST", "/v2/orders", {
                        "symbol": symbol, "qty": str(qty),
                        "side": "sell", "type": "limit",
                        "limit_price": str(sell_price),
                        "time_in_force": "day",
                    })
                    log(f"✅ LIMIT SELL {symbol} {qty} @ ${sell_price} — {reason}")
                    return True
        except: pass
        # Fallback to market
        alpaca("DELETE", f"/v2/positions/{symbol}")
        log(f"✅ MARKET SELL {symbol} — {reason}")
        return True
    except Exception as e:
        log(f"❌ Sell {symbol}: {e}")
        return False

def check_exit_conditions(positions):
    """
    Strategy-aware exit system.
    Each position uses whichever strategy was assigned at entry:
    Strategy A: Fixed 7% take-profit + 4% stop + no time limit
    Strategy B: Trailing stop + 4% hard stop + 3-day time stop
    """
    today = datetime.now().strftime("%Y-%m-%d")

    for pos in positions:
        symbol       = pos["symbol"]
        pnl_pct      = float(pos["unrealized_plpc"])
        pnl_usd      = float(pos["unrealized_pl"])
        current_price = float(pos["current_price"])
        owner        = "Claude" if symbol in shared_state["claude_positions"] else "Grok"

        # Get exit config for this position
        exit_cfg  = shared_state["position_exits"].get(symbol, {})
        strategy  = exit_cfg.get("strategy", "A")
        entry_price = exit_cfg.get("entry_price", current_price)
        entry_date  = exit_cfg.get("entry_date", today)
        trail_pct   = exit_cfg.get("trail_pct", RULES["exit_B_trail_default"])

        # ── UNIVERSAL: Hard stop-loss (both strategies) ───────
        if pnl_pct <= -RULES["exit_A_stop_loss"]:
            log(f"🛑 [{owner}] STOP LOSS {symbol} ({pnl_pct*100:.1f}%) strategy={strategy}")
            if smart_sell(symbol, "stop loss", pos):
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
            continue

        # ── STRATEGY A: Fixed take-profit ────────────────────
        if strategy == "A":
            if pnl_pct >= RULES["exit_A_take_profit"]:
                log(f"🎯 [A] [{owner}] FIXED TP {symbol} +{pnl_pct*100:.1f}% >= {RULES['exit_A_take_profit']*100:.0f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, "strategy A take-profit", pos):
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
            else:
                log(f"   [A] {symbol}: {pnl_pct*100:+.2f}% → target {RULES['exit_A_take_profit']*100:.0f}% | holding")

        # ── STRATEGY B: Trailing stop + time stop ────────────
        elif strategy == "B":
            # Update peak price
            if current_price > exit_cfg.get("peak_price", entry_price):
                old_peak = exit_cfg.get("peak_price", entry_price)
                shared_state["position_exits"][symbol]["peak_price"] = current_price
                log(f"   [B] {symbol}: New peak ${current_price:.2f} (was ${old_peak:.2f}) | trailing stop = ${current_price*(1-trail_pct):.2f}")

            peak_price   = shared_state["position_exits"][symbol].get("peak_price", current_price)
            trail_stop   = peak_price * (1 - trail_pct)
            profit_at_peak = (peak_price - entry_price) / entry_price

            # Trailing activates only once position is +3% profitable
            trail_active = profit_at_peak >= RULES["exit_B_trail_activates"]

            if trail_active and current_price <= trail_stop:
                log(f"🎯 [B] [{owner}] TRAILING STOP {symbol} | "
                    f"peak=${peak_price:.2f} trail=${trail_stop:.2f} current=${current_price:.2f} | "
                    f"+{pnl_pct*100:.1f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, f"strategy B trailing stop (peak ${peak_price:.2f})", pos):
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)

            # Time stop — sell if stuck after N days
            elif RULES["exit_B_time_stop_days"]:
                try:
                    days_held = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
                    if days_held >= RULES["exit_B_time_stop_days"] and pnl_pct < RULES["exit_B_trail_activates"]:
                        log(f"⏰ [B] [{owner}] TIME STOP {symbol} | "
                            f"{days_held} days held, only {pnl_pct*100:+.2f}% — freeing capital")
                        if smart_sell(symbol, f"strategy B time stop ({days_held} days)", pos):
                            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                            shared_state["position_exits"].pop(symbol, None)
                    else:
                        status = f"trailing active, peak=${peak_price:.2f} stop=${trail_stop:.2f}" if trail_active else f"waiting for +3% to activate trail (currently {pnl_pct*100:+.2f}%)"
                        log(f"   [B] {symbol}: {status} | {days_held}d held")
                except: pass

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
- SPY TREND: {shared_state.get('spy_trend','neutral').upper()} {'— DO NOT suggest new buys, exits only' if shared_state.get('spy_trend') == 'bear' else '— Full trading active'}
- AUTONOMOUS trades: technicals + news + politician signals + HOT IPOs (>5% momentum)
- COLLABORATIVE candidates: Biggest gainers (>3%) AND triple confirmation stocks
- IPOs with strong momentum are GOOD autonomous trades — they move fast
- Never suggest a biggest gainer for autonomous — only flag for collaborative
- Politician mimicking: 2+ politicians bought = strong autonomous signal
- Triple confirmation = best collaborative candidate
- LIMIT ORDERS: Bot uses limit orders at bid/ask midpoint for better entry price

Propose up to 2 AUTONOMOUS trades from your budget.
Also flag any COLLABORATIVE candidates (biggest gainers or strong politician signals).
JSON: {{"strategy_name":"name","market_thesis":"brief","proposed_trades":[{{"action":"buy/sell/short","symbol":"TICK","notional_usd":15.0,"confidence":85,"direction":"long/short","signals":["s1","s2","s3"],"rationale":"brief","politician_signal":false,"ipo_signal":false}}],"collaborative_candidates":[{{"symbol":"TICK","reason":"biggest gainer OR triple confirmation","confidence":90}}],"bearish_watchlist":["tickers"]}}"""

    log("🔵 Round 1 — Claude proposing...")
    log("🔴 Round 1 — Grok proposing...")

    c_ok = shared_state["claude_healthy"]
    g_ok = shared_state["grok_healthy"]

    if c_ok:
        claude_r1 = safe_ask_claude(r1_prompt,
            "You are Claude, disciplined quant trader. ONLY valid JSON under 500 chars.")
    else:
        log("⚠️ Claude unhealthy — skipping Round 1 for Claude")
        claude_r1 = None

    if g_ok:
        grok_r1 = safe_ask_grok(r1_prompt,
            "You are Grok, momentum trader with Twitter access. ONLY valid JSON under 500 chars.")
    else:
        log("⚠️ Grok unhealthy — skipping Round 1 for Grok")
        grok_r1 = None

    # Single AI failover — one AI runs solo
    if claude_r1 and not grok_r1:
        log("⚠️ FAILOVER: Grok down — Claude running solo this cycle")
    elif grok_r1 and not claude_r1:
        log("⚠️ FAILOVER: Claude down — Grok running solo this cycle")

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
                # ── SMART ENTRY: Limit order below market for better fill ──
                # Get current price and set limit slightly below ask
                snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
                snap_headers = {
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET
                }
                limit_price = None
                try:
                    snap_res = requests.get(snap_url, headers=snap_headers, timeout=5)
                    if snap_res.ok:
                        quote     = snap_res.json().get("quote", {})
                        ask_price = float(quote.get("ap", 0))
                        bid_price = float(quote.get("bp", 0))
                        if ask_price > 0 and bid_price > 0:
                            # Set limit at midpoint between bid and ask
                            # This gets a better fill than market while still executing
                            mid_price   = round((ask_price + bid_price) / 2, 2)
                            limit_price = mid_price
                            spread_pct  = round((ask_price - bid_price) / ask_price * 100, 3)
                            log(f"   📊 {symbol} bid=${bid_price} ask=${ask_price} "
                                f"spread={spread_pct}% → limit=${limit_price}")
                except Exception as eq:
                    log(f"   ⚠️ Quote fetch failed for {symbol}: {eq} — using market order")

                # Use limit order if we got a price, otherwise fall back to market
                if limit_price and limit_price > 0:
                    # Convert notional to shares for limit order
                    shares = round(notional / limit_price, 6)
                    if shares > 0:
                        order = alpaca("POST", "/v2/orders", {
                            "symbol":        symbol,
                            "qty":           str(shares),
                            "side":          "buy",
                            "type":          "limit",
                            "limit_price":   str(limit_price),
                            "time_in_force": "day",  # Cancels if not filled by close
                        })
                        log(f"✅ LIMIT BUY [{owner}] {symbol} {shares} shares @ ${limit_price} "
                            f"(~${notional:.2f}) | conf={conf}% | {order['id'][:8]}...")
                    else:
                        raise Exception("Calculated 0 shares")
                else:
                    # Fallback to market order
                    order = alpaca("POST", "/v2/orders", {
                        "symbol": symbol, "notional": str(round(notional, 2)),
                        "side": "buy", "type": "market", "time_in_force": "day",
                    })
                    log(f"✅ MARKET BUY [{owner}] {symbol} ${notional:.2f} | "
                        f"conf={conf}% | fee≈${fee_est:.3f} | {order['id'][:8]}...")

                remaining_cash -= notional; new_positions += 1
                if owner == "claude" and symbol not in shared_state["claude_positions"]:
                    shared_state["claude_positions"].append(symbol)
                elif owner == "grok" and symbol not in shared_state["grok_positions"]:
                    shared_state["grok_positions"].append(symbol)
                pos_symbols.append(symbol)

                # ── ASSIGN EXIT STRATEGY ──────────────────
                # Get current price for entry tracking
                try:
                    bars = get_bars(symbol, days=10)
                    ind  = compute_indicators(bars) if bars else None
                    entry_px = limit_price if limit_price else (ind["close"] if ind else notional/10)
                    is_collab = trade.get("is_collab", False)

                    if is_collab:
                        # Collaborative trades: use strategy stored in trade data
                        # (agreed by both AIs in Round 3)
                        strat   = trade.get("exit_strategy", "A")
                        rationale = trade.get("exit_rationale", "collaborative trade")
                    else:
                        # Autonomous trades: AI decides based on signals
                        strat, rationale = decide_exit_strategy_solo(
                            symbol, trade, bars, ind
                        )
                    assign_exit_strategy(symbol, strat, entry_px,
                                        trade.get("confidence", 80), rationale)
                except Exception as ex:
                    log(f"⚠️ Exit strategy assign failed: {ex} — defaulting to A")
                    assign_exit_strategy(symbol, "A", notional/10, 80, "default")

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



def classify_ai_error(error_str):
    """
    Classify what kind of failure this is.
    Credit exhaustion needs different handling than a timeout.
    """
    e = str(error_str).lower()
    if any(x in e for x in ["credit", "billing", "quota", "insufficient_quota",
                              "rate_limit", "429", "payment", "balance", "overloaded"]):
        return "credits_exhausted"
    if any(x in e for x in ["timeout", "connection", "network", "resolve", "unreachable"]):
        return "network_error"
    if any(x in e for x in ["401", "403", "invalid_api_key", "authentication"]):
        return "auth_error"
    return "unknown_error"

def safe_ask_claude(prompt, system, retries=3):
    """
    Claude with full health tracking.
    Detects: credit exhaustion, network errors, auth errors.
    On credits exhausted → immediately hands off to Grok (no retries).
    On network error → retries then marks unhealthy.
    Auto-recovers after 30 min for network errors.
    Credit exhaustion needs manual top-up — logs clear warning.
    """
    try:
        result = ask_with_retry(ask_claude, prompt, system, retries=retries)
        if result:
            shared_state["claude_healthy"]    = True
            shared_state["claude_fail_count"] = 0
            shared_state["claude_fail_reason"] = None
        else:
            shared_state["claude_fail_count"] += 1
            if shared_state["claude_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["claude_healthy"]   = False
                shared_state["last_claude_fail"] = datetime.now().isoformat()
                log(f"⚠️ Claude UNHEALTHY — Grok taking over")
        return result
    except Exception as e:
        error_type = classify_ai_error(str(e))
        shared_state["claude_fail_count"]  += 1
        shared_state["claude_fail_reason"]  = error_type

        if error_type == "credits_exhausted":
            # Immediate handoff — no point retrying
            shared_state["claude_healthy"]    = False
            shared_state["claude_credits_ok"] = False
            shared_state["last_claude_fail"]  = datetime.now().isoformat()
            log(f"💳 CLAUDE CREDITS EXHAUSTED — Immediate handoff to Grok")
            log(f"   ACTION REQUIRED: Top up Anthropic API credits at console.anthropic.com")
            log(f"   Grok will handle ALL trading until Claude credits are restored")
        elif error_type == "auth_error":
            shared_state["claude_healthy"]   = False
            shared_state["last_claude_fail"] = datetime.now().isoformat()
            log(f"🔑 CLAUDE AUTH ERROR — Check ANTHROPIC_KEY in Railway variables")
        else:
            if shared_state["claude_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["claude_healthy"]   = False
                shared_state["last_claude_fail"] = datetime.now().isoformat()
                log(f"❌ Claude UNHEALTHY ({error_type}) — Grok taking over")
        return None

def safe_ask_grok(prompt, system, retries=3):
    """
    Grok with full health tracking.
    Same logic as Claude — detects credit exhaustion vs network errors.
    On credits exhausted → immediately hands off to Claude (no retries).
    """
    try:
        result = ask_with_retry(ask_grok, prompt, system, retries=retries)
        if result:
            shared_state["grok_healthy"]    = True
            shared_state["grok_fail_count"] = 0
            shared_state["grok_fail_reason"] = None
        else:
            shared_state["grok_fail_count"] += 1
            if shared_state["grok_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["grok_healthy"]   = False
                shared_state["last_grok_fail"] = datetime.now().isoformat()
                log(f"⚠️ Grok UNHEALTHY — Claude taking over")
        return result
    except Exception as e:
        error_type = classify_ai_error(str(e))
        shared_state["grok_fail_count"]  += 1
        shared_state["grok_fail_reason"]  = error_type

        if error_type == "credits_exhausted":
            shared_state["grok_healthy"]    = False
            shared_state["grok_credits_ok"] = False
            shared_state["last_grok_fail"]  = datetime.now().isoformat()
            log(f"💳 GROK CREDITS EXHAUSTED — Immediate handoff to Claude")
            log(f"   ACTION REQUIRED: Top up xAI API credits at console.x.ai")
            log(f"   Claude will handle ALL trading until Grok credits are restored")
        elif error_type == "auth_error":
            shared_state["grok_healthy"]   = False
            shared_state["last_grok_fail"] = datetime.now().isoformat()
            log(f"🔑 GROK AUTH ERROR — Check GROK_KEY in Railway variables")
        else:
            if shared_state["grok_fail_count"] >= RULES["failover_max_retries"]:
                shared_state["grok_healthy"]   = False
                shared_state["last_grok_fail"] = datetime.now().isoformat()
                log(f"❌ Grok UNHEALTHY ({error_type}) — Claude taking over")
        return None

def check_ai_health():
    """
    Check AI health. Handle 3 failure types differently:
    - credits_exhausted: No auto-recovery (needs manual top-up)
    - network_error:     Auto-recover after 30 min
    - auth_error:        No auto-recovery (needs key fix)
    """
    now = datetime.now()

    for ai in ["claude", "grok"]:
        last_fail   = shared_state.get(f"last_{ai}_fail")
        fail_reason = shared_state.get(f"{ai}_fail_reason")
        credits_ok  = shared_state.get(f"{ai}_credits_ok", True)

        if last_fail and not shared_state[f"{ai}_healthy"]:
            fail_time = datetime.fromisoformat(last_fail)
            mins_down = (now - fail_time).seconds // 60

            if fail_reason == "credits_exhausted":
                # No auto-recovery — needs manual top-up
                log(f"💳 {ai.title()} credits exhausted ({mins_down} min down) — MANUAL TOP-UP NEEDED")
                log(f"   {'console.anthropic.com' if ai == 'claude' else 'console.x.ai'}")
                continue

            elif fail_reason == "auth_error":
                log(f"🔑 {ai.title()} auth error ({mins_down} min down) — Check API key in Railway")
                continue

            elif (now - fail_time).seconds >= 1800:  # 30 min for network errors
                shared_state[f"{ai}_healthy"]    = True
                shared_state[f"{ai}_fail_count"] = 0
                shared_state[f"{ai}_fail_reason"] = None
                log(f"🔄 {ai.title()} auto-recovered after {mins_down} min — retrying")

    c_ok = shared_state["claude_healthy"]
    g_ok = shared_state["grok_healthy"]
    c_reason = shared_state.get("claude_fail_reason","")
    g_reason = shared_state.get("grok_fail_reason","")

    if c_ok and g_ok:
        mode = None
        log(f"✅ Both AIs healthy — full collaboration")
    elif c_ok and not g_ok:
        mode = "claude_only"
        log(f"⚠️ FAILOVER: Grok down ({g_reason}) — Claude running solo")
        if g_reason == "credits_exhausted":
            log(f"   💳 Top up xAI credits → console.x.ai to restore Grok")
    elif g_ok and not c_ok:
        mode = "grok_only"
        log(f"⚠️ FAILOVER: Claude down ({c_reason}) — Grok running solo")
        if c_reason == "credits_exhausted":
            log(f"   💳 Top up Anthropic credits → console.anthropic.com to restore Claude")
    else:
        mode = "autopilot"
        log(f"🆘 BOTH AIs down — AUTOPILOT MODE")
        log(f"   Claude: {c_reason} | Grok: {g_reason}")

    shared_state["failover_mode"] = mode
    return c_ok, g_ok, mode


def get_cash_thresholds(equity):
    """
    Dynamic cash thresholds that scale with account size.
    Bigger account = higher bar before both AIs activate.
    This prevents wasting API calls on tiny trades relative to account size.

    Scaling:
      Sleep threshold:  Always $8 (absolute minimum tradeable)
      Watch threshold:  max($20, equity × 5%)
      Active threshold: watch × 1.2

    Examples:
      $55   equity → sleep=$8  watch=$20  active=$20  (floor)
      $500  equity → sleep=$8  watch=$25  active=$30
      $1,000 equity → sleep=$8  watch=$50  active=$60
      $2,000 equity → sleep=$8  watch=$100 active=$120
      $5,000 equity → sleep=$8  watch=$250 active=$300
      $10,000 equity → sleep=$8 watch=$500 active=$600
    """
    sleep_thresh  = RULES["cash_sleep_threshold"]  # Always $8
    watch_thresh  = max(
        RULES["threshold_floor"],
        round(equity * RULES["threshold_equity_pct"], 2)
    )
    active_thresh = round(watch_thresh * RULES["threshold_active_mult"], 2)

    return {
        "sleep":  sleep_thresh,
        "watch":  watch_thresh,
        "active": active_thresh,
    }

def run_autopilot(positions, pos_symbols, cash, equity):
    """
    Rule-based autopilot — fires when BOTH AIs are unavailable.
    Uses pure technical signals only — no AI calls needed.
    Conservative: only acts on very clear signals.

    BUY signal:  RSI < 35 AND MACD positive AND price > SMA20
    SELL signal: RSI > 70 OR stop-loss hit OR take-profit hit
    """
    log("🤖 AUTOPILOT MODE — Pure technical rules, no AI calls")
    log(f"   Rules: BUY if RSI<{RULES['autopilot_rsi_buy']} + MACD+ | SELL if RSI>{RULES['autopilot_rsi_sell']}")

    # Check exits first
    for pos in positions:
        symbol  = pos["symbol"]
        pnl_pct = float(pos["unrealized_plpc"])
        if pnl_pct >= RULES["take_profit_pct"]:
            log(f"🎯 AUTOPILOT take-profit: {symbol} +{pnl_pct*100:.1f}%")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ AUTOPILOT SOLD {symbol}")
            except Exception as e: log(f"❌ {e}")
        elif pnl_pct <= -RULES["stop_loss_pct"]:
            log(f"🛑 AUTOPILOT stop-loss: {symbol} {pnl_pct*100:.1f}%")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ AUTOPILOT SOLD {symbol}")
            except Exception as e: log(f"❌ {e}")

    # Only look for buys if we have enough cash
    if cash < RULES["cash_active_threshold"]:
        log(f"⏳ AUTOPILOT: Cash ${cash:.2f} below threshold — monitoring only")
        return

    # Scan universe for clear technical buy signals
    open_count = len(alpaca("GET", "/v2/positions"))
    if open_count >= RULES["max_positions"]:
        log(f"⏳ AUTOPILOT: Max positions reached — holding")
        return

    best_signal = None
    best_score  = 0

    for sym in RULES["universe"]:
        if sym in pos_symbols: continue
        bars = get_bars(sym)
        ind  = compute_indicators(bars)
        if not ind: continue

        score = 0
        signals = []

        # RSI oversold
        if ind["rsi"] and ind["rsi"] < RULES["autopilot_rsi_buy"]:
            score += 3; signals.append(f"RSI={ind['rsi']} oversold")

        # MACD positive
        if ind["macd"] and ind["macd"] > 0:
            score += 2; signals.append(f"MACD={ind['macd']} positive")

        # Price above SMA20 (uptrend)
        if ind["sma20"] and ind["close"] > ind["sma20"]:
            score += 1; signals.append(f"price>${ind['sma20']} SMA20")

        # 5-day momentum positive
        if ind["mom_5d"] and ind["mom_5d"] > 0:
            score += 1; signals.append(f"mom5d={ind['mom_5d']}%")

        if score >= 5 and score > best_score:  # Need 5+ for autopilot buy
            best_score  = score
            best_signal = {"symbol": sym, "score": score, "signals": signals, "ind": ind}

    if best_signal:
        sym      = best_signal["symbol"]
        notional = min(cash * 0.40, cash - 5)  # Conservative 40% of cash
        if notional >= 8:
            log(f"🤖 AUTOPILOT BUY: {sym} score={best_score} signals={best_signal['signals']}")
            try:
                order = alpaca("POST", "/v2/orders", {
                    "symbol": sym, "notional": str(round(notional, 2)),
                    "side": "buy", "type": "market", "time_in_force": "day",
                })
                log(f"✅ AUTOPILOT BUY {sym} ${notional:.2f} | {order['id'][:8]}...")
                shared_state["claude_positions"].append(sym)  # Assign to Claude by default
            except Exception as e: log(f"❌ Autopilot buy {sym}: {e}")
    else:
        log(f"🤖 AUTOPILOT: No clear buy signals found — holding cash safely")

def run_watch_mode(cash, equity, positions, pos_symbols):
    """
    LEGACY — No longer called in normal operation.
    New system: both AIs sleep together, bot monitors alone.
    Kept as emergency fallback only.
    Dynamic watch mode — single AI (Claude) monitors until cash hits active threshold.
    If cash crosses active threshold mid-watch, Claude calls Grok immediately.

    Thresholds scale with equity:
      $55   account → watch=$20,  active=$24
      $500  account → watch=$25,  active=$30
      $1,000 account → watch=$50,  active=$60
      $5,000 account → watch=$250, active=$300
    """
    thresholds    = get_cash_thresholds(equity)
    watch_thresh  = thresholds["watch"]
    active_thresh = thresholds["active"]

    log(f"👁️ WATCH MODE (Cash: ${cash:.2f} | Equity: ${equity:.2f})")
    log(f"   Thresholds — watch=${watch_thresh:.2f} | active=${active_thresh:.2f}")
    log(f"   Claude monitoring solo until cash >= ${active_thresh:.2f}")

    # Auto stop-loss / take-profit first (no AI call needed)
    check_exit_conditions(positions)
    positions   = alpaca("GET", "/v2/positions")
    pos_symbols = [p["symbol"] for p in positions]

    # Refresh cash after potential sells
    account     = alpaca("GET", "/v2/account")
    current_cash = float(account["cash"])

    # ── CHECK: Has cash crossed active threshold? ──────────
    if current_cash >= active_thresh:
        log(f"💡 Cash ${current_cash:.2f} crossed active threshold ${active_thresh:.2f}!")
        log(f"   Waking Grok — resuming full collaboration NOW")
        # Return to caller — run_cycle will see cash >= active and run full session
        shared_state["watch_mode_active"] = False
        return

    if not positions:
        log(f"👁️ Watch mode: No positions — waiting for cash to hit ${active_thresh:.2f}")
        return

    # ── CLAUDE SOLO WATCH — 1 API call ────────────────────
    pos_summary = [
        f"  {p['symbol']}: {round(float(p['unrealized_plpc'])*100,2):+.2f}% "
        f"(${round(float(p['unrealized_pl']),2):+.2f}) "
        f"owner={'Claude' if p['symbol'] in shared_state['claude_positions'] else 'Grok'}"
        for p in positions
    ]

    watch_prompt = f"""WATCH MODE — Cash ${current_cash:.2f} (need ${active_thresh:.2f} to wake Grok).
Account equity: ${equity:.2f}

OPEN POSITIONS:
{chr(10).join(pos_summary)}

TASKS (conservative — 1 API call only):
1. Any position showing deterioration? (approaching -4% stop-loss)
2. Any position ready to take profit? (near +7%)
3. Estimate when cash might recover (from dividends, sells, or deposits)
4. What is the top priority buy when Grok wakes at ${active_thresh:.2f}?

SELL only if:
  - Position clearly failing and stop-loss imminent
  - Selling now saves more than waiting for auto-stop

JSON: {{"action":"hold/sell","sell_symbol":"TICKER or none",
"sell_reason":"brief","positions_health":"good/concern",
"concern_symbol":"TICKER or none",
"priority_buy_at_wakeup":"{shared_state.get('next_buy_target','best signal')}",
"estimated_cash_recovery":"brief"}}"""

    decision = safe_ask_claude(watch_prompt,
        "You are Claude in conservative watch mode. ONLY valid JSON under 400 chars.")

    if decision:
        action       = decision.get("action","hold").lower()
        sell_symbol  = decision.get("sell_symbol","none").upper()
        health       = decision.get("positions_health","good")
        concern      = decision.get("concern_symbol","none")
        priority_buy = decision.get("priority_buy_at_wakeup","")
        cash_eta     = decision.get("estimated_cash_recovery","")

        log(f"👁️ Claude watch: health={health} | action={action}")
        if concern and concern != "NONE":
            log(f"⚠️ Concern flagged: {concern} — monitoring closely")
        if priority_buy:
            log(f"📋 Priority buy when Grok wakes: {priority_buy}")
            shared_state["next_buy_target"] = priority_buy
        if cash_eta:
            log(f"⏱️ Cash recovery estimate: {cash_eta}")

        if action == "sell" and sell_symbol != "NONE" and sell_symbol in pos_symbols:
            log(f"👁️ Claude watch sell: {sell_symbol} — {decision.get('sell_reason','')}")
            try:
                alpaca("DELETE", f"/v2/positions/{sell_symbol}")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != sell_symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != sell_symbol]
                log(f"✅ WATCH SOLD {sell_symbol}")

                # Recheck cash — might have crossed active threshold
                account2 = alpaca("GET", "/v2/account")
                new_cash  = float(account2["cash"])
                if new_cash >= active_thresh:
                    log(f"🚀 Cash ${new_cash:.2f} now >= ${active_thresh:.2f} — WAKING GROK!")
                    log(f"   Resuming full collaboration next cycle")
                    shared_state["watch_mode_active"] = False
            except Exception as e: log(f"❌ Watch sell {sell_symbol}: {e}")
        else:
            log(f"👁️ Watch: HOLD — next full collab at ${active_thresh:.2f} cash")

    log(f"💰 API saved: 1 call used vs 5 normal | Cash: ${current_cash:.2f} / ${active_thresh:.2f} to wake Grok")

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

    # ── AI HEALTH CHECK ─────────────────────────────────────
    c_ok, g_ok, failover_mode = check_ai_health()

    # ── DYNAMIC CASH THRESHOLDS ──────────────────────────────
    thresholds       = get_cash_thresholds(equity)
    sleep_threshold  = thresholds["sleep"]
    watch_threshold  = thresholds["watch"]
    active_threshold = thresholds["active"]
    has_positions    = open_count > 0

    log(f"💵 Cash thresholds — sleep=${sleep_threshold} watch=${watch_threshold:.2f} active=${active_threshold:.2f}")

    # TIER 1: No cash AND no positions — fully flat
    if cash < sleep_threshold and not has_positions:
        log(f"😴 CASH SLEEP (${cash:.2f} < ${sleep_threshold}) — No AI calls, fully flat")
        return

    # TIER 2: No cash but has positions — profit-taking mode
    if cash < sleep_threshold and has_positions:
        log(f"💸 LOW CASH (${cash:.2f}) — Profit-taking focus")
        run_low_cash_cycle(positions, pos_symbols, cash, equity, features)
        return

    # TIER 3: Between sleep and active — both AIs sleep, bot monitors alone
    if sleep_threshold <= cash < active_threshold:
        log(f"😴 BOT AUTONOMOUS (${cash:.2f}) — Both AIs sleeping until cash >= ${active_threshold:.2f}")
        log(f"   Bot monitoring {len(positions)} positions | Zero AI calls")
        shared_state["watch_mode_active"] = False
        shared_state["ai_sleeping"]       = True
        if not shared_state.get("sleep_reason"):
            shared_state["sleep_reason"] = f"cash ${cash:.2f} below threshold ${active_threshold:.2f}"
        # Bot just runs autonomous monitor — no AI calls at all
        fires = run_autonomous_monitor(positions, pos_symbols, cash, equity)
        if fires > 0:
            log(f"   Bot executed {fires} autonomous exit(s)")
        log(f"   Next wake: cash >= ${active_threshold:.2f} | 2+ stops | 4pm review | 8:30am research")
        log(f"   API saved: 0 calls used vs 5 normal")
        return

    # Cash is sufficient for full collaboration
    shared_state["watch_mode_active"] = False

    # TIER 4: Both AIs down — autopilot
    if failover_mode == "autopilot":
        log(f"🆘 AUTOPILOT — Both AIs down, pure technical rules")
        run_autopilot(positions, pos_symbols, cash, equity)
        return

    # ── SPY TREND FILTER ─────────────────────────────────────
    spy_trend, spy_price, spy_sma50, spy_change = get_spy_trend()
    log(f"📈 SPY trend: {spy_trend.upper()} | price=${spy_price:.2f} vs SMA50=${spy_sma50:.2f} | 5d={spy_change:+.2f}%")

    if spy_trend == "bear":
        log(f"🐻 BEAR MARKET FILTER — SPY below SMA50")
        log(f"   New buys paused. Managing existing positions only.")
        log(f"   Existing stops and take-profits still active.")
        # Still run check_exit_conditions but skip new buys
        # We signal this to collaborative_session via shared_state
        shared_state["spy_trend"] = "bear"
    else:
        shared_state["spy_trend"] = spy_trend
        if spy_trend == "bull":
            log(f"🐂 BULL MARKET — Full trading active")

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
        # No trades = nothing to monitor → AIs stay awake for next cycle
    else:
        execute_trades(final_trades, cash, pos_symbols, open_count, final_plan, features)

        # ── Generate trading brief then sleep ───────────────
        positions_after = alpaca("GET", "/v2/positions")
        acct_after      = alpaca("GET", "/v2/account")
        cash_after      = float(acct_after["cash"])
        eq_after        = float(acct_after["equity"])
        pool_after      = get_trading_pool(eq_after)

        if positions_after or cash_after < get_cash_thresholds(eq_after)["active"]:
            log("📋 AIs writing trading brief before sleeping...")
            try:
                # Get fresh intelligence for the brief
                news_b  = get_news_context()
                mkt_b   = get_market_context()
                chart_b = get_chart_section()
                pol_b, pol_trades_b = get_politician_trades()
                inv_b, inv_hold_b   = get_top_investor_portfolios()
                gainers_b = get_biggest_gainers()
                ipos_b    = get_recent_ipos()
                smart_b   = analyze_smart_money(
                    analyze_politician_signals(pol_trades_b, chart_b),
                    inv_hold_b, gainers_b
                )
                generate_trading_brief(
                    eq_after, cash_after, positions_after, pool_after,
                    chart_b, news_b, mkt_b, pol_b, inv_b,
                    gainers_b, ipos_b, smart_b
                )
            except Exception as be:
                log(f"⚠️ Brief generation failed: {be} — bot uses default rules")

            ai_sleep(f"brief written — bot monitoring {len(positions_after)} positions + watchlist")
        else:
            log("⏳ No positions, sufficient cash — AIs staying awake for next opportunity")

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



# ══════════════════════════════════════════════════════════════
# TRADING BRIEF SYSTEM
# AIs write a full brief before sleeping.
# Bot reads the brief and executes precisely.
# ══════════════════════════════════════════════════════════════

def generate_trading_brief(equity, cash, positions, pool,
                            chart_section, news, market_ctx,
                            pol_text, inv_text, gainers, ipos, smart_money):
    """
    Both AIs collaborate to write a complete trading brief.
    This is the LAST thing AIs do before sleeping.
    The bot follows this brief precisely while AIs sleep.

    Brief contains:
    1. Account-level direction and rules
    2. Per-position exit strategies and notes
    3. Watchlist — what to buy when cash is available
    4. Collaborative targets for big-ticket trades
    """
    log("=" * 55)
    log("📋 GENERATING TRADING BRIEF — AIs writing instructions for bot")
    log("=" * 55)

    pos_summary = []
    for p in positions:
        sym     = p["symbol"]
        pnl_pct = round(float(p["unrealized_plpc"]) * 100, 2)
        owner   = "Claude" if sym in shared_state["claude_positions"] else "Grok"
        pos_summary.append(f"  {sym} [{owner}]: {pnl_pct:+.2f}% entry=${float(p['avg_entry_price']):.2f}")

    thresholds   = get_cash_thresholds(equity)
    spy_trend, spy_price, spy_sma50, spy_chg = get_spy_trend()
    triple_syms  = smart_money.get("triple_confirmation", [])
    top_collab   = smart_money.get("top_collab", [])
    hot_ipos     = [i["symbol"] for i in ipos if abs(i.get("mom_5d",0)) > 5]

    # ── PHASE 1: Claude writes account + position brief ──────
    claude_brief_prompt = f"""You are CLAUDE — writing the trading brief for the bot to follow while you sleep.

CURRENT STATE:
Equity: ${equity:.2f} | Cash: ${cash:.2f} | SPY: {spy_trend.upper()} ({spy_chg:+.2f}%)
Pool: Claude=${pool['claude']:.2f} | Grok=${pool['grok']:.2f} | Reserve=${pool['reserve']:.2f}
Open positions:
{chr(10).join(pos_summary) if pos_summary else '  None'}

INTELLIGENCE:
Politicians: {pol_text[:200]}
Top investors: {inv_text[:150]}
Triple confirmation: {triple_syms}
Hot IPOs: {hot_ipos}
Gainers: {[(g['symbol'],f'+{g["change"]:.1f}%') for g in gainers[:5]]}
Market: {market_ctx}
News: {news[:200]}
Indicators: {chart_section[:400]}

Write YOUR PART of the trading brief (account rules + position notes).
Be specific — the bot follows this EXACTLY with no AI to ask.

JSON (keep under 600 chars):
{{
  "market_bias": "bullish/bearish/neutral",
  "risk_level": "low/medium/high",
  "max_new_trades": 2,
  "spy_rule": "no_buy_bear/trade_all/only_bull",
  "daily_target_pct": 2.0,
  "stop_day_loss_pct": 5.0,
  "position_notes": {{
    "SYMBOL": {{
      "strategy": "A/B",
      "conviction": "high/medium/low",
      "thesis": "brief",
      "special_rule": "e.g. sell before earnings/dont hold overnight",
      "trail_pct": 0.05
    }}
  }},
  "claude_watchlist": [
    {{"symbol":"TICK","why":"brief","entry_max":0,"strategy":"A/B","confidence":85}}
  ],
  "account_notes": "any special instructions for bot"
}}"""

    # ── PHASE 2: Grok writes momentum + watchlist brief ──────
    grok_brief_prompt = f"""You are GROK — writing the trading brief for the bot to follow while you sleep.

CURRENT STATE:
Equity: ${equity:.2f} | Cash: ${cash:.2f} | SPY: {spy_trend.upper()} ({spy_chg:+.2f}%)
Open positions:
{chr(10).join(pos_summary) if pos_summary else '  None'}

MOMENTUM INTELLIGENCE:
Hot IPOs: {[(i['symbol'],f"mom={i['mom_5d']}%",f"{i['days_old']}d old") for i in ipos[:5]]}
Biggest gainers: {[(g['symbol'],f'+{g["change"]:.1f}%') for g in gainers[:5]]}
News: {news[:200]}

Write YOUR PART of the trading brief (momentum picks + IPO watchlist).
The bot executes your watchlist automatically when cash is available.

JSON (keep under 600 chars):
{{
  "market_sentiment": "bullish/bearish/neutral",
  "momentum_strength": "strong/moderate/weak",
  "position_notes": {{
    "SYMBOL": {{
      "strategy": "A/B",
      "conviction": "high/medium/low",
      "thesis": "brief",
      "special_rule": "brief",
      "trail_pct": 0.05
    }}
  }},
  "grok_watchlist": [
    {{"symbol":"TICK","why":"IPO momentum/gainer","entry_max":0,"strategy":"B","confidence":85}}
  ],
  "collab_targets": [
    {{"symbol":"TICK","condition":"both 95%+ on next wake","why":"triple confirmation"}}
  ],
  "sentiment_notes": "key sentiment insight for bot context"
}}"""

    claude_brief = None
    grok_brief   = None

    try:
        claude_brief = safe_ask_claude(claude_brief_prompt,
            "You are Claude writing a precise trading brief. ONLY valid JSON under 600 chars.")
        if claude_brief:
            log(f"🔵 Claude brief: bias={claude_brief.get('market_bias')} "
                f"risk={claude_brief.get('risk_level')} "
                f"watchlist={[w.get('symbol') for w in claude_brief.get('claude_watchlist',[])]}")
    except Exception as e:
        log(f"❌ Claude brief: {e}")

    try:
        grok_brief = safe_ask_grok(grok_brief_prompt,
            "You are Grok writing a precise trading brief. ONLY valid JSON under 600 chars.")
        if grok_brief:
            log(f"🔴 Grok brief: sentiment={grok_brief.get('market_sentiment')} "
                f"momentum={grok_brief.get('momentum_strength')} "
                f"watchlist={[w.get('symbol') for w in grok_brief.get('grok_watchlist',[])]}")
    except Exception as e:
        log(f"❌ Grok brief: {e}")

    # ── PHASE 3: Merge both briefs into unified brief ─────────
    if not claude_brief and not grok_brief:
        log("⚠️ Both briefs failed — bot will use default rules only")
        return

    # Build merged watchlist (no duplicates)
    merged_watchlist = []
    seen_watchlist   = set()

    c_watch = (claude_brief or {}).get("claude_watchlist", [])
    g_watch = (grok_brief   or {}).get("grok_watchlist",  [])

    for item in c_watch + g_watch:
        sym = item.get("symbol","")
        if sym and sym not in seen_watchlist and sym not in [p["symbol"] for p in positions]:
            merged_watchlist.append({**item, "owner": "claude" if item in c_watch else "grok"})
            seen_watchlist.add(sym)

    # Merge position notes
    merged_pos_notes = {}
    c_pos = (claude_brief or {}).get("position_notes", {})
    g_pos = (grok_brief   or {}).get("position_notes", {})
    for sym in set(list(c_pos.keys()) + list(g_pos.keys())):
        merged_pos_notes[sym] = {**(c_pos.get(sym,{})), **(g_pos.get(sym,{}))}
        # Update stored exit strategy with brief's instructions
        if sym in shared_state["position_exits"]:
            if "strategy" in merged_pos_notes[sym]:
                shared_state["position_exits"][sym]["strategy"]  = merged_pos_notes[sym]["strategy"]
            if "trail_pct" in merged_pos_notes[sym]:
                shared_state["position_exits"][sym]["trail_pct"] = merged_pos_notes[sym]["trail_pct"]
            if "special_rule" in merged_pos_notes[sym]:
                shared_state["position_exits"][sym]["ai_notes"]  = merged_pos_notes[sym]["special_rule"]

    # Collab targets from Grok
    collab_targets = (grok_brief or {}).get("collab_targets", [])

    # Store the complete brief
    shared_state["trading_brief"] = {
        "account": {
            "market_bias":    (claude_brief or {}).get("market_bias", "neutral"),
            "risk_level":     (claude_brief or {}).get("risk_level", "medium"),
            "max_new_trades": (claude_brief or {}).get("max_new_trades", 2),
            "spy_rule":       (claude_brief or {}).get("spy_rule", "no_buy_bear"),
            "daily_target":   (claude_brief or {}).get("daily_target_pct", 2.0) / 100,
            "stop_day_if":    -(claude_brief or {}).get("stop_day_loss_pct", 5.0) / 100,
            "brief_date":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "brief_notes":    (claude_brief or {}).get("account_notes","") + " | " +
                              (grok_brief   or {}).get("sentiment_notes",""),
        },
        "positions":      merged_pos_notes,
        "watchlist":      merged_watchlist,
        "collab_targets": collab_targets,
    }

    # Also update sleeping_strategies with AI notes
    for sym, notes in merged_pos_notes.items():
        if sym in shared_state["sleeping_strategies"]:
            shared_state["sleeping_strategies"][sym]["ai_notes"] = notes.get("special_rule","")
            shared_state["sleeping_strategies"][sym]["conviction"] = notes.get("conviction","medium")

    log(f"📋 TRADING BRIEF COMPLETE:")
    log(f"   Market bias: {shared_state['trading_brief']['account']['market_bias'].upper()}")
    log(f"   Risk level:  {shared_state['trading_brief']['account']['risk_level'].upper()}")
    log(f"   Watchlist:   {[w['symbol'] for w in merged_watchlist]}")
    log(f"   Collab targets: {[t.get('symbol') for t in collab_targets]}")
    log(f"   Position rules: {list(merged_pos_notes.keys())}")
    log(f"   Bot notes: {shared_state['trading_brief']['account']['brief_notes'][:100]}")
    log("=" * 55)

def execute_watchlist(cash, equity, pos_symbols, open_count):
    """
    Bot executes watchlist trades autonomously.
    No AI needed — follows the brief exactly.
    Runs while AIs are sleeping.
    """
    brief     = shared_state["trading_brief"]
    watchlist = brief.get("watchlist", [])
    account_b = brief.get("account", {})
    max_trades = account_b.get("max_new_trades", 2)
    risk_level = account_b.get("risk_level", "medium")
    spy_rule   = account_b.get("spy_rule", "no_buy_bear")
    thresholds = get_cash_thresholds(equity)

    if not watchlist:
        return 0
    if cash < thresholds["active"]:
        return 0
    if open_count >= RULES["max_positions"]:
        return 0

    # Check SPY rule
    spy_trend, _, _, _ = get_spy_trend()
    if spy_rule == "no_buy_bear" and spy_trend == "bear":
        log(f"🐻 Watchlist paused — SPY bearish (brief rule: {spy_rule})")
        return 0

    # Risk-based position sizing
    size_pct = {"low": 0.25, "medium": 0.35, "high": 0.45}.get(risk_level, 0.35)

    trades_made  = 0
    remaining_cash = cash

    for item in watchlist:
        if trades_made >= max_trades:
            break
        if open_count + trades_made >= RULES["max_positions"]:
            break

        sym        = item.get("symbol","")
        entry_max  = float(item.get("entry_max", 99999))
        strategy   = item.get("strategy","A")
        confidence = item.get("confidence",80)
        owner      = item.get("owner","shared")
        why        = item.get("why","")

        if not sym or sym in pos_symbols:
            continue
        if confidence < RULES["min_confidence"]:
            continue

        # Check current price vs entry_max
        try:
            bars = get_bars(sym, days=5)
            if not bars:
                continue
            current_price = bars[-1]["c"]
            if entry_max > 0 and current_price > entry_max:
                log(f"⏭️ WATCHLIST: {sym} at ${current_price:.2f} > entry max ${entry_max:.2f} — waiting")
                continue
        except:
            continue

        notional = min(remaining_cash * size_pct, remaining_cash - 5)
        if notional < 8:
            log(f"⚠️ WATCHLIST: Not enough cash for {sym} (${remaining_cash:.2f})")
            break

        log(f"📋 WATCHLIST EXECUTE: {sym} — {why[:60]}")
        log(f"   Price=${current_price:.2f} entry_max=${entry_max:.2f} size=${notional:.2f} strategy={strategy}")

        try:
            # Use limit order at midpoint
            snap_url = f"{DATA_URL}/v2/stocks/{sym}/quotes/latest"
            headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
            limit_price = None
            try:
                snap_res = requests.get(snap_url, headers=headers, timeout=5)
                if snap_res.ok:
                    quote = snap_res.json().get("quote",{})
                    bid   = float(quote.get("bp",0))
                    ask   = float(quote.get("ap",0))
                    if bid > 0 and ask > 0 and (entry_max == 0 or ask <= entry_max):
                        limit_price = round((bid+ask)/2, 2)
            except: pass

            if limit_price:
                shares = round(notional / limit_price, 6)
                order  = alpaca("POST", "/v2/orders", {
                    "symbol": sym, "qty": str(shares),
                    "side": "buy", "type": "limit",
                    "limit_price": str(limit_price),
                    "time_in_force": "day",
                })
                log(f"✅ WATCHLIST BUY {sym} {shares} @ ${limit_price} | owner={owner} | {order['id'][:8]}...")
            else:
                order = alpaca("POST", "/v2/orders", {
                    "symbol": sym, "notional": str(round(notional,2)),
                    "side": "buy", "type": "market", "time_in_force": "day",
                })
                log(f"✅ WATCHLIST MARKET BUY {sym} ${notional:.2f} | {order['id'][:8]}...")

            remaining_cash -= notional
            trades_made    += 1
            pos_symbols.append(sym)

            # Assign to owner
            if owner == "claude":
                shared_state["claude_positions"].append(sym)
            elif owner == "grok":
                shared_state["grok_positions"].append(sym)

            # Assign exit strategy from brief
            entry_px = limit_price or current_price
            assign_exit_strategy(sym, strategy, entry_px, confidence,
                                 f"watchlist: {why[:60]}")

        except Exception as e:
            log(f"❌ Watchlist buy {sym}: {e}")

    if trades_made > 0:
        log(f"📋 Watchlist executed {trades_made} trade(s) autonomously (AIs sleeping)")
    return trades_made

# ══════════════════════════════════════════════════════════════
# AI SLEEP / WAKE SYSTEM
# Bot runs fully autonomous while AIs sleep.
# AIs only wake on specific triggers — saves 96% of API calls.
# ══════════════════════════════════════════════════════════════

def ai_sleep(reason="trades executed — waiting for cash threshold"):
    """Put both AIs to sleep. Bot takes over autonomous execution."""
    shared_state["ai_sleeping"]     = True
    shared_state["sleep_reason"]    = reason
    shared_state["last_sleep_time"] = datetime.now().isoformat()
    shared_state["wake_reason"]     = None
    shared_state["stops_fired_today"] = 0

    # Store current exit strategies so bot can execute them while AIs sleep
    shared_state["sleeping_strategies"] = dict(shared_state["position_exits"])

    log(f"😴 AIs going to SLEEP — {reason}")
    log(f"   Bot running autonomously with stored strategies")
    log(f"   Positions covered: {list(shared_state['sleeping_strategies'].keys()) or 'none'}")
    log(f"   Wake conditions:")
    log(f"     1. Cash crosses active threshold")
    log(f"     2. All positions closed + cash available")
    log(f"     3. 2+ stop-losses fire (market emergency)")
    log(f"     4. 8:30am premarket (always)")
    log(f"     5. SPY drops >2% suddenly (market crash guard)")

def ai_wake(reason):
    """Wake both AIs — they resume full analysis and decision making."""
    was_sleeping = shared_state["ai_sleeping"]
    shared_state["ai_sleeping"]    = False
    shared_state["wake_reason"]    = reason
    shared_state["last_wake_time"] = datetime.now().isoformat()

    if was_sleeping:
        sleep_time = shared_state.get("last_sleep_time","")
        if sleep_time:
            try:
                slept_mins = (datetime.now() - datetime.fromisoformat(sleep_time)).seconds // 60
                log(f"🌅 AIs WAKING UP — {reason}")
                log(f"   Slept for {slept_mins} minutes")
                log(f"   Bot executed {shared_state['stops_fired_today']} stop/TP autonomously while sleeping")
            except:
                log(f"🌅 AIs WAKING UP — {reason}")
        else:
            log(f"🌅 AIs WAKING UP — {reason}")

def check_wake_conditions(cash, equity, positions, spy_change=0):
    """
    Check if any wake condition is met.
    Returns (should_wake, reason) tuple.
    No AI calls — pure logic and Alpaca data.
    """
    if not shared_state["ai_sleeping"]:
        return False, None

    thresholds = get_cash_thresholds(equity)

    # ── WAKE CONDITION 1: Cash crossed active threshold ──────
    if cash >= thresholds["active"]:
        return True, f"cash ${cash:.2f} crossed active threshold ${thresholds['active']:.2f}"

    # ── WAKE CONDITION 2: Fully in cash + available ───────────
    if len(positions) == 0 and cash >= thresholds["sleep"]:
        return True, f"all positions closed — ${cash:.2f} cash available"

    # ── WAKE CONDITION 3: 2+ stop-losses fired ────────────────
    if shared_state["stops_fired_today"] >= 2:
        return True, f"EMERGENCY — {shared_state['stops_fired_today']} stop-losses fired, market may be crashing"

    # ── WAKE CONDITION 4: SPY flash crash guard ───────────────
    if spy_change <= -2.0:
        return True, f"EMERGENCY — SPY dropped {spy_change:.1f}% (flash crash guard)"

    return False, None

def run_autonomous_monitor(positions, pos_symbols, cash, equity):
    """
    Fully autonomous bot operation while AIs sleep.
    Executes stored exit strategies with zero AI calls.
    Just pure rule-based execution.
    """
    if not positions:
        return 0

    stops_fired = 0
    log(f"🤖 BOT AUTONOMOUS — {len(positions)} positions | Both AIs sleeping | 0 API calls")

    for pos in positions:
        symbol        = pos["symbol"]
        pnl_pct       = float(pos["unrealized_plpc"])
        pnl_usd       = float(pos["unrealized_pl"])
        current_price = float(pos["current_price"])
        pos_value     = float(pos["market_value"])

        # Get stored strategy for this position
        strategy_cfg = shared_state["sleeping_strategies"].get(
            symbol,
            shared_state["position_exits"].get(symbol, {})
        )
        strategy     = strategy_cfg.get("strategy", "A")
        entry_price  = strategy_cfg.get("entry_price", current_price)
        entry_date   = strategy_cfg.get("entry_date", datetime.now().strftime("%Y-%m-%d"))
        trail_pct    = strategy_cfg.get("trail_pct", get_trail_pct(symbol))
        ai_notes     = strategy_cfg.get("ai_notes", "")

        # Update peak price
        if current_price > strategy_cfg.get("peak_price", entry_price):
            strategy_cfg["peak_price"] = current_price
            shared_state["sleeping_strategies"][symbol] = strategy_cfg
            shared_state["position_exits"][symbol]      = strategy_cfg

        peak_price = strategy_cfg.get("peak_price", entry_price)

        log(f"   [{strategy}] {symbol}: {pnl_pct*100:+.2f}% (${pnl_usd:+.2f}) "
            f"peak=${peak_price:.2f} | {ai_notes[:40] if ai_notes else ''}")

        sold = False

        # ── UNIVERSAL: Hard stop-loss ─────────────────────────
        if pnl_pct <= -RULES["exit_A_stop_loss"]:
            log(f"🛑 AUTO STOP-LOSS {symbol} {pnl_pct*100:.1f}% — bot executing (AIs sleeping)")
            if smart_sell(symbol, "autonomous stop-loss", pos):
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
                shared_state["sleeping_strategies"].pop(symbol, None)
                shared_state["stops_fired_today"] += 1
                stops_fired += 1
                sold = True

        # ── STRATEGY A: Fixed take-profit ─────────────────────
        elif strategy == "A" and not sold:
            if pnl_pct >= RULES["exit_A_take_profit"]:
                log(f"🎯 AUTO TAKE-PROFIT [A] {symbol} +{pnl_pct*100:.1f}% — bot executing")
                if smart_sell(symbol, "autonomous strategy A take-profit", pos):
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
                    shared_state["sleeping_strategies"].pop(symbol, None)
                    sold = True

        # ── STRATEGY B: Trailing stop ─────────────────────────
        elif strategy == "B" and not sold:
            profit_at_peak  = (peak_price - entry_price) / entry_price
            trail_active    = profit_at_peak >= RULES["exit_B_trail_activates"]
            trail_stop      = peak_price * (1 - trail_pct)

            if trail_active and current_price <= trail_stop:
                log(f"🎯 AUTO TRAILING STOP [B] {symbol} "
                    f"peak=${peak_price:.2f} stop=${trail_stop:.2f} "
                    f"current=${current_price:.2f} | +{pnl_pct*100:.1f}%")
                if smart_sell(symbol, f"autonomous strategy B trailing stop", pos):
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
                    shared_state["sleeping_strategies"].pop(symbol, None)
                    sold = True

            # Time stop
            elif RULES["exit_B_time_stop_days"] and not sold:
                try:
                    days_held = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
                    if days_held >= RULES["exit_B_time_stop_days"] and pnl_pct < RULES["exit_B_trail_activates"]:
                        log(f"⏰ AUTO TIME STOP [B] {symbol} — {days_held}d held, only {pnl_pct*100:+.2f}%")
                        if smart_sell(symbol, f"autonomous time stop ({days_held}d)", pos):
                            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                            shared_state["position_exits"].pop(symbol, None)
                            shared_state["sleeping_strategies"].pop(symbol, None)
                            sold = True
                except: pass

    # ── Execute watchlist if cash became available ───────────
    try:
        acct_w  = alpaca("GET", "/v2/account")
        cash_w  = float(acct_w["cash"])
        eq_w    = float(acct_w["equity"])
        pos_w   = alpaca("GET", "/v2/positions")
        sym_w   = [p["symbol"] for p in pos_w]
        thresh_w = get_cash_thresholds(eq_w)
        if cash_w >= thresh_w["active"] and len(pos_w) < RULES["max_positions"]:
            watchlist_count = len(shared_state["trading_brief"].get("watchlist",[]))
            if watchlist_count > 0:
                log(f"📋 Cash ${cash_w:.2f} available + {watchlist_count} watchlist items — executing")
                wl_trades = execute_watchlist(cash_w, eq_w, sym_w, len(pos_w))
                if wl_trades > 0:
                    stops_fired = -1  # Signal that new trades were made (not stops)
    except Exception as we:
        log(f"⚠️ Watchlist check error: {we}")

    return stops_fired

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

    # ── Background cash monitor (no AI needed) ───────────────
    cash_check_interval = 60   # Check cash every 60 seconds silently
    last_cash_check     = 0
    last_known_cash     = 0

    while True:
        try:
            mode, interval = get_market_mode()
            now_et   = datetime.now(ZoneInfo("America/New_York"))
            today    = now_et.date()
            now_unix = time.time()

            # ── SILENT CASH MONITOR (no AI, no cost) ─────────
            # Runs every 60 seconds regardless of market mode
            # Only during market hours to save Alpaca API calls
            mins_et = now_et.hour * 60 + now_et.minute
            is_market_hours = 510 <= mins_et < 1020  # 8:30am-5pm ET

            if is_market_hours and (now_unix - last_cash_check) >= cash_check_interval:
                try:
                    account      = alpaca("GET", "/v2/account")
                    current_cash = float(account["cash"])
                    current_eq   = float(account["equity"])
                    thresholds   = get_cash_thresholds(current_eq)
                    last_cash_check = now_unix

                    # Cash increased past active threshold — wake both AIs
                    if (shared_state["watch_mode_active"] and
                        current_cash >= thresholds["active"] and
                        last_known_cash < thresholds["active"]):
                        log(f"💡 CASH MONITOR: ${current_cash:.2f} crossed active threshold "
                            f"${thresholds['active']:.2f} — WAKING GROK immediately!")
                        log(f"   No AI was needed to detect this — pure Alpaca API check")
                        shared_state["watch_mode_active"] = False
                        # Force immediate full cycle
                        if mode in ("opening","prime","power_hour"):
                            log(f"🚀 Triggering immediate full collaboration cycle!")
                            run_cycle()
                            last_cash_check = time.time()

                    # Cash dropped to sleep level — log silently
                    elif (current_cash < thresholds["sleep"] and
                          last_known_cash >= thresholds["sleep"]):
                        log(f"💤 CASH MONITOR: Cash dropped to ${current_cash:.2f} — entering sleep mode")

                    # Cash entered watch zone
                    elif (current_cash < thresholds["active"] and
                          last_known_cash >= thresholds["active"] and
                          current_cash >= thresholds["sleep"]):
                        log(f"👁️ CASH MONITOR: Cash ${current_cash:.2f} entered watch zone "
                            f"(${thresholds['sleep']}-${thresholds['active']:.2f})")
                        log(f"   Switching to Claude-only monitoring")
                        shared_state["watch_mode_active"] = True

                    last_known_cash = current_cash

                except Exception as ce:
                    pass  # Silent — cash monitor never crashes the main loop

            # ── MAIN TRADING LOGIC ───────────────────────────

            if mode == "sleep":
                next_check = (now_et + timedelta(minutes=interval)).strftime("%H:%M ET")
                log(f"😴 Sleeping {interval} min. Next: {next_check}")

            elif mode == "premarket":
                # Always wake AIs at 8:30am for research regardless of sleep state
                if shared_state["ai_sleeping"]:
                    ai_wake("8:30am premarket — daily research always runs")
                run_premarket()
                # After premarket research, AIs sleep again if no cash to trade
                try:
                    acct_check = alpaca("GET", "/v2/account")
                    cash_check = float(acct_check["cash"])
                    eq_check   = float(acct_check["equity"])
                    thresh_check = get_cash_thresholds(eq_check)
                    if cash_check < thresh_check["active"]:
                        ai_sleep("premarket research done — both AIs sleeping, bot takes over")
                except: pass

            elif mode in ("opening", "prime", "power_hour"):
                labels = {"opening":"🔔 OPENING","prime":"🚀 PRIME","power_hour":"⚡ POWER HOUR"}

                if shared_state["ai_sleeping"]:
                    # ── BOT AUTONOMOUS MODE ───────────────────
                    # AIs sleeping — bot monitors and executes stored strategies
                    try:
                        acct      = alpaca("GET", "/v2/account")
                        cash_now  = float(acct["cash"])
                        eq_now    = float(acct["equity"])
                        pos_now   = alpaca("GET", "/v2/positions")
                        sym_now   = [p["symbol"] for p in pos_now]

                        # Check SPY for crash guard
                        spy_trend_now, spy_price_now, spy_sma_now, spy_chg_now = get_spy_trend()

                        # Check wake conditions
                        should_wake, wake_reason = check_wake_conditions(
                            cash_now, eq_now, pos_now, spy_chg_now
                        )

                        if should_wake:
                            ai_wake(wake_reason)
                            log(f"{labels[mode]} — AIs woke up, running collaboration")
                            run_cycle()
                        else:
                            # Bot executes stored strategies autonomously
                            log(f"🤖 {labels[mode]} — BOT AUTONOMOUS (AIs sleeping)")
                            log(f"   Cash: ${cash_now:.2f} | Positions: {sym_now or 'none'}")
                            log(f"   Sleep reason: {shared_state['sleep_reason']}")
                            fires = run_autonomous_monitor(pos_now, sym_now, cash_now, eq_now)
                            if fires > 0:
                                log(f"   {fires} position(s) exited autonomously")
                    except Exception as ae:
                        log(f"❌ Autonomous monitor error: {ae}")
                else:
                    # AIs awake — full collaboration
                    log(f"{labels[mode]} — Full collaboration")
                    run_cycle()

            elif mode == "afterhours":
                # Always wake for afterhours review
                if shared_state["ai_sleeping"]:
                    ai_wake("4pm afterhours — daily review always runs")
                run_afterhours()
                # After review, AIs sleep again if nothing to do
                try:
                    acct_ah   = alpaca("GET", "/v2/account")
                    cash_ah   = float(acct_ah["cash"])
                    eq_ah     = float(acct_ah["equity"])
                    thresh_ah = get_cash_thresholds(eq_ah)
                    pos_ah    = alpaca("GET", "/v2/positions")
                    if cash_ah < thresh_ah["active"] and pos_ah:
                        ai_sleep("afterhours done — both AIs sleeping, bot guards overnight")
                    elif not pos_ah and cash_ah < thresh_ah["active"]:
                        ai_sleep("afterhours done — no positions, both AIs sleeping")
                except: pass

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
