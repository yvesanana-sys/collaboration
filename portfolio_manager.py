"""
portfolio_manager.py — NovaTrade Portfolio Manager
═══════════════════════════════════════════════════
Trade history persistence, gains tracking, autonomy tiers,
pool calculations, rebalancing, and projection accuracy.

All volume paths live here. Injected by bot_with_proxy.py.
"""

import os
import json
import time
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── Volume file paths ─────────────────────────────────────────
TRADE_HISTORY_FILE  = "/data/trade_history.json"
SHARED_STATE_FILE   = "/data/shared_state.json"
SLEEP_STATE_FILE    = "/data/sleep_state.json"

# Keys to persist across redeploys
_PERSIST_KEYS = [
    "day_start_equity", "week_start_equity", "month_start_equity", "year_start_equity",
    "last_rebalance_day", "last_rebalance_week", "last_reset_month", "last_reset_year",
    "crypto_day_start", "crypto_last_day", "crypto_month_start", "crypto_year_start",
    "claude_total_pnl", "grok_total_pnl", "claude_allocation", "grok_allocation",
    "claude_win_days", "grok_win_days", "proj_hit_count", "proj_total_count",
    "day_trade_count", "day_trade_dates", "stops_fired_today",
]

# ── Shared references (injected by bot) ──────────────────────
log            = print
shared_state   = {}
trade_history  = []
RULES          = {}
prompt_builder = None
alpaca         = None


def _set_context(log_fn, shared_state_ref, trade_history_ref,
                 rules, alpaca_fn=None, prompt_builder_ref=None):
    """Called by bot to inject all dependencies."""
    global log, shared_state, trade_history, RULES, alpaca, prompt_builder
    log            = log_fn
    shared_state   = shared_state_ref
    trade_history  = trade_history_ref
    RULES          = rules
    if alpaca_fn:           alpaca         = alpaca_fn
    if prompt_builder_ref:  prompt_builder = prompt_builder_ref


def track_projection_accuracy(symbol, actual_high, actual_low):
    """
    Compare yesterday's projection vs today's actual prices.
    Updates shared_state accuracy counters.
    Call once per position at end of day from run_afterhours().
    """
    proj = shared_state.get("last_projections", {}).get(symbol)
    if not proj or proj.get("error") or not proj.get("proj_high"):
        return
    ph = proj["proj_high"]
    pl = proj["proj_low"]
    # "Hit" = actual range stayed within 2% of projected range
    high_hit = actual_high <= ph * 1.02
    low_hit  = actual_low  >= pl * 0.98
    both_hit = high_hit and low_hit
    shared_state["proj_total_count"] += 1
    if both_hit:
        shared_state["proj_hit_count"] += 1
    total = shared_state["proj_total_count"]
    hits  = shared_state["proj_hit_count"]
    shared_state["proj_accuracy_pct"] = round(hits / total * 100, 1) if total else 0.0
    log(f"📐 Proj accuracy: {symbol} | Proj={pl}–{ph} "
        f"Actual={round(actual_low,2)}–{round(actual_high,2)} | "
        f"{'✅ HIT' if both_hit else '❌ MISS'} | "
        f"Rolling: {hits}/{total} = {shared_state['proj_accuracy_pct']}%")

ALPACA_KEY    = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
GROK_KEY      = os.environ.get("GROK_KEY", "")
BINANCE_KEY   = os.environ.get("BINANCE_KEY", "")    # Binance.US crypto
BINANCE_SECRET= os.environ.get("BINANCE_SECRET", "") # Binance.US crypto
BASE_URL      = "https://api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets"
BOT_NAME      = os.environ.get("BOT_NAME", "collaboration")

# ── GitHub Auto-Deploy ────────────────────────────────────────
# Set these in Railway environment variables to enable auto-push.
# GITHUB_TOKEN: Personal Access Token with repo write access
#   → github.com → Settings → Developer settings → Personal access tokens
#   → Generate new token (classic) → check 'repo' scope
# GITHUB_REPO: owner/repo format e.g. "hanz/novatrade"
# GITHUB_BRANCH: branch to push to (default: main)
# [GitHub constants → github_deploy.py]

RULES = {
    "total_budget":          55,
    "growth_reserve_pct":    0.15,   # Always keep 15% untouched
    "trading_pool_pct":      0.85,
    "daily_loss_limit_pct":  0.05,
    "max_positions":         2,      # Concentrate — 1-2 positions max at small equity
    # ── Stop / Profit targets (swing strategy) ───────────────
    # Old: 4% stop / 7% TP — too tight, gets shaken out constantly
    # New: 10% stop / 20% TP — swing trades need room to breathe
    "stop_loss_pct":         0.10,   # -10% stop (wider = fewer premature exits)
    "take_profit_pct":       0.20,   # +20% TP base
    "min_confidence":          75,   # Lowered slightly for more entries
    "collab_min_confidence":   90,
    "collab_min_signals":      4,
    "collab_require_news":     True,
    "collab_min_profit_pct":   0.10, # Min 10% expected profit
    # ── Collaborative big-ticket gatekeeper ──
    "collab_unlock_equity":    3000,
    "collab_min_trade_size":   1000,
    "collab_max_trade_pct":    0.40,
    "interval_minutes":        5,
    # ── Exit strategies (updated for swing trading) ──────────
    # Strategy A: Fixed TP (good for breakout entries)
    "exit_A_take_profit":      0.20,  # 20% fixed TP (was 7%)
    "exit_A_stop_loss":        0.10,  # 10% stop (was 4%)
    "exit_A_time_stop_days":   5,     # 5-day time stop
    # Strategy B: Trailing (lets big winners run further)
    "exit_B_trail_default":    0.08,  # 8% trail below peak (was 5%)
    "exit_B_trail_volatile":   0.12,  # 12% trail for TSLA/MSTR/COIN
    "exit_B_trail_stable":     0.06,  # 6% trail for AAPL/MSFT
    "exit_B_trail_activates":  0.10,  # Trail activates at +10% (was +3%)
    "exit_B_stop_loss":        0.10,  # 10% hard stop
    "exit_B_time_stop_days":   5,     # 5-day time stop
    # ── Tier-based position sizing (stocks) ──────────────────
    # More aggressive at small equity, shrinks as account grows
    "stock_tiers": [
        {"min_equity":   0, "max_equity": 150,  "risk_pct": 0.35, "max_pos": 1,
         "focus": ["TSLA", "NVDA"],
         "note": "Tier 1 — 35% risk, 1 position, TSLA/NVDA only"},
        {"min_equity": 150, "max_equity": 300,  "risk_pct": 0.30, "max_pos": 2,
         "focus": ["TSLA", "NVDA", "AMD"],
         "note": "Tier 2 — 30% risk, add AMD"},
        {"min_equity": 300, "max_equity": 600,  "risk_pct": 0.25, "max_pos": 2,
         "focus": ["TSLA", "NVDA", "AMD", "META", "PLTR"],
         "note": "Tier 3 — 25% risk, 5 stocks"},
        {"min_equity": 600, "max_equity": 9999, "risk_pct": 0.20, "max_pos": 3,
         "focus": None,  # Full universe
         "note": "Tier 4 — 20% risk, full universe"},
    ],
    # Volatile stocks (wider trail needed)
    "volatile_stocks": ["TSLA","MSTR","COIN","RKLB","SOFI","AMD","NVDA"],
    # Stable stocks (tighter trail)
    "stable_stocks":   ["AAPL","MSFT","GOOGL","AMZN","META"],
    # ── Breakout entry parameters ─────────────────────────────
    "breakout_periods":       20,     # 20-period high breakout
    "vol_spike_multiplier":   1.5,    # Volume must be 1.5x average
    "rsi_momentum_min":       55,     # RSI > 55 for momentum entry
    "rsi_oversold_max":       35,     # RSI < 35 for dip entry
    # ── Drawdown protection ────────────────────────────────────
    "global_drawdown_pause":  0.40,   # Pause if equity drops 40%
    # ── Cash threshold tiers ──────────────────────────────────
    "cash_sleep_threshold":    8,
    "cash_watch_base_pct":     0.04,
    "cash_active_base_pct":    0.05,
    "threshold_floor":         20,
    "threshold_equity_pct":    0.05,
    "threshold_active_mult":   1.5,   # Increased from 1.1 — prevents spurious wake-ups (self-repair 2026-04-09)
    # ── AI failover ──
    "failover_max_retries":    3,
    "autopilot_rsi_buy":       35,
    "autopilot_rsi_sell":      70,
    # Feature unlock thresholds
    "short_sell_threshold":  2000,
    "options_threshold":     5000,
    "full_margin_threshold": 25000,
    # Performance allocation
    "base_split":            0.50,
    "max_allocation":        0.70,
    "min_allocation":        0.30,
    "performance_window":    7,
    # Autonomy tiers
    "autonomy_tiers": [
        {"equity": 150,  "autonomous_fund": 50,  "collab_floor": 50,  "description": "Tier 1 — $25 Claude + $25 Grok autonomous | $50+ collaborative"},
        {"equity": 300,  "autonomous_fund": 100, "collab_floor": 100, "description": "Tier 2 — $50 Claude + $50 Grok autonomous | $100+ collaborative"},
        {"equity": 600,  "autonomous_fund": 200, "collab_floor": 200, "description": "Tier 3 — $100 Claude + $100 Grok autonomous | $200+ collaborative"},
        {"equity": 1200, "autonomous_fund": 400, "collab_floor": 400, "description": "Tier 4 — $200 Claude + $200 Grok autonomous | $400+ collaborative"},
        {"equity": 2000, "autonomous_fund": 600, "collab_floor": 600, "description": "Tier 5 — $300 Claude + $300 Grok autonomous | $600+ collaborative + shorts"},
    ],
    "universe": [
        # Tier 1 (high-vol, big moves): TSLA first, NVDA second
        "TSLA","NVDA",
        # Tier 2 additions
        "AMD","META","PLTR",
        # Tier 3 additions
        "AMZN","SOFI","MSTR","COIN","RKLB",
        # Tier 4 (full universe)
        "AAPL","MSFT","GOOGL","NFLX","CRM",
    ],
}



# ── Trade History Log ─────────────────────────────────────
# Persists in memory; last 500 trades kept
# Each entry: buy or sell with full context for performance review

SHARED_STATE_FILE   = "/data/shared_state.json"
SLEEP_STATE_FILE    = "/data/sleep_state.json"

# Keys worth persisting across redeploys — equity baselines, P&L periods
_PERSIST_KEYS = ['day_start_equity', 'week_start_equity', 'month_start_equity', 'year_start_equity', 'last_rebalance_day', 'last_rebalance_week', 'last_reset_month', 'last_reset_year', 'crypto_day_start', 'crypto_last_day', 'crypto_month_start', 'crypto_year_start', 'claude_total_pnl', 'grok_total_pnl', 'claude_allocation', 'grok_allocation', 'claude_win_days', 'grok_win_days', 'proj_hit_count', 'proj_total_count', 'day_trade_count', 'day_trade_dates', 'stops_fired_today']

def _load_trade_history() -> list:
    for path in [TRADE_HISTORY_FILE, "./trade_history.json"]:
        try:
            with open(path, "r") as f:
                data = json.load(f)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📈 Loaded {len(data)} trades from {path}", flush=True)
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return []

def _save_trade_history(history: list):
    for path in [TRADE_HISTORY_FILE, "./trade_history.json"]:
        try:
            with open(path, "w") as f:
                json.dump(history, f)
            return
        except Exception:
            continue

trade_history = _load_trade_history()
_load_shared_state()    # Restore equity baselines, P&L periods
_load_sleep_state()     # Restore AI sleep/wake state
# Replay closed trades into prompt_builder memory — AI lessons persist across redeploys
try:
    _replay_trade_history_into_memory()
except Exception:
    pass

def record_trade(action, symbol, qty, price, notional, owner,
                 confidence=None, reason=None, pnl_usd=None,
                 pnl_pct=None, strategy=None, entry_price=None,
                 spy_trend=None):
    """
    Record every buy/sell to trade_history for /history endpoint.
    action  : 'buy' | 'sell' | 'short' | 'stop_loss' | 'take_profit' | 'trail_stop' | 'time_stop'
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    entry = {
        "time":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_et":      now_et.strftime("%Y-%m-%d %H:%M ET"),
        "action":       action,
        "symbol":       symbol,
        "qty":          qty,
        "price":        round(float(price), 4) if price else None,
        "notional":     round(float(notional), 2) if notional else None,
        "owner":        owner,          # claude | grok | shared | bot
        "confidence":   confidence,
        "reason":       reason,
        # Exit-only fields
        "entry_price":  round(float(entry_price), 4) if entry_price else None,
        "pnl_usd":      round(float(pnl_usd), 2) if pnl_usd is not None else None,
        "pnl_pct":      round(float(pnl_pct) * 100, 2) if pnl_pct is not None else None,
        "strategy":     strategy,       # A | B | autopilot
        "spy_trend":    spy_trend or shared_state.get("spy_trend", "neutral"),
        "equity_after": None,           # filled in async — best effort
    }
    trade_history.append(entry)
    # Keep last 500
    if len(trade_history) > 500:
        trade_history.pop(0)
    try:
        _save_trade_history(trade_history)
    except Exception:
        pass

    # ── Feed prompt memory on every closed trade ──────────────
    # Sells/stops/TPs carry pnl — buys don't, so filter on pnl_usd presence
    if pnl_usd is not None and action in (
        "sell", "stop_loss", "take_profit", "trail_stop", "time_stop"
    ):
        try:
            prompt_builder.on_trade_closed(
                symbol       = symbol,
                pnl_usd      = float(pnl_usd),
                pnl_pct      = float(pnl_pct) if pnl_pct is not None else 0.0,
                owner        = owner or "bot",
                strategy     = strategy or "A",
                signals      = [],          # signals not available here; memory still useful
                spy_trend    = spy_trend or shared_state.get("spy_trend", "neutral"),
                entry_reason = reason or "",
            )
        except Exception:
            pass  # Never let memory errors break trade recording

    return entry

def _load_shared_state():
    """Load persisted shared_state from volume on boot."""
    for path in [SHARED_STATE_FILE, "./shared_state.json"]:
        try:
            with open(path) as f:
                data = json.load(f)
            shared_state.update({k: v for k, v in data.items() if k in _PERSIST_KEYS})
            log(f"📂 Shared state loaded: {len(data)} keys restored from {path}")
            return
        except Exception:
            continue

def _save_shared_state():
    """Persist critical shared_state keys to volume."""
    try:
        data = {k: shared_state[k] for k in _PERSIST_KEYS if k in shared_state}
        for path in [SHARED_STATE_FILE, "./shared_state.json"]:
            try:
                with open(path, "w") as f:
                    json.dump(data, f)
                return
            except Exception:
                continue
    except Exception as e:
        log(f"⚠️ shared_state save failed: {e}")

def _save_sleep_state():
    """Persist AI sleep/wake state so bot survives redeploys gracefully."""
    try:
        data = {
            "ai_sleeping":          shared_state.get("ai_sleeping", False),
            "sleep_reason":         shared_state.get("sleep_reason", ""),
            "last_sleep_time":      shared_state.get("last_sleep_time", ""),
            "wake_reason":          shared_state.get("wake_reason", ""),
            "ai_wake_instructions": shared_state.get("ai_wake_instructions", []),
            "trading_brief":        shared_state.get("trading_brief", ""),
            "tomorrows_plan":       shared_state.get("tomorrows_plan", ""),
        }
        for path in [SLEEP_STATE_FILE, "./sleep_state.json"]:
            try:
                with open(path, "w") as f:
                    json.dump(data, f)
                return
            except Exception:
                continue
    except Exception as e:
        log(f"⚠️ sleep_state save failed: {e}")

def _load_sleep_state():
    """Load AI sleep/wake state on boot — bot knows if AIs were sleeping."""
    for path in [SLEEP_STATE_FILE, "./sleep_state.json"]:
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.items():
                shared_state[k] = v
            was_sleeping = data.get("ai_sleeping", False)
            if was_sleeping:
                log(f"💤 Restored: AIs were sleeping ({data.get('sleep_reason','')})")
                log(f"   Wake instructions: {len(data.get('ai_wake_instructions',[]))} items")
            else:
                log(f"📂 Sleep state restored: AIs were awake")
            return
        except Exception:
            continue

TRADE_HISTORY_FILE = "/data/trade_history.json"  # Railway persistent volume — survives redeploys

def _save_all_persistent_state():
    """Save all persistent state to volume in one call."""
    try:
        _save_shared_state()
        _save_sleep_state()
    except Exception as e:
        log(f"⚠️ Persistent state save error: {e}")

def _trim_trade_history_to_6months():
    """
    Keep only last 6 months of trade history — keeps data relevant.
    Older patterns from different market regimes are misleading.
    Called once daily at reset time.
    """
    global trade_history
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    before = len(trade_history)
    trade_history = [
        t for t in trade_history
        if datetime.fromisoformat(
            t.get("time", "2020-01-01T00:00:00Z").replace("Z", "+00:00")
        ) >= cutoff
    ]
    trimmed = before - len(trade_history)
    if trimmed > 0:
        log(f"🗂️ Trade history trimmed: removed {trimmed} entries older than 6 months "
            f"({len(trade_history)} remaining)")
        try:
            _save_trade_history(trade_history)
        except Exception:
            pass

# [get_market_context → moved to market_data.py / intelligence.py]
# [_edgar_quarter → moved to market_data.py / intelligence.py]
# [_fetch_edgar_politician_form4 → moved to market_data.py / intelligence.py]
# [get_politician_trades → moved to market_data.py / intelligence.py]
# [analyze_politician_signals → moved to market_data.py / intelligence.py]
# [get_biggest_gainers → moved to market_data.py / intelligence.py]
# [get_recent_ipos → moved to market_data.py / intelligence.py]
# [get_top_investor_portfolios → moved to market_data.py / intelligence.py]
# [analyze_smart_money → moved to market_data.py / intelligence.py]

def _replay_trade_history_into_memory():
    """
    On boot, replay closed trades from volume into prompt_builder memory.
    This makes AI lessons truly persistent — survives all redeploys.
    Only replays sells/exits (not buys) since those have P&L data.
    """
    global prompt_builder
    if not trade_history:
        return
    exit_actions = {"sell", "stop_loss", "take_profit", "trail_stop", "time_stop"}
    replayed = 0
    try:
        for trade in trade_history:
            if trade.get("action") not in exit_actions:
                continue
            if trade.get("pnl_usd") is None:
                continue
            try:
                prompt_builder.on_trade_closed(
                    symbol       = trade.get("symbol", ""),
                    pnl_usd      = float(trade.get("pnl_usd", 0)),
                    pnl_pct      = float(trade.get("pnl_pct", 0)),
                    owner        = trade.get("owner", "bot"),
                    strategy     = trade.get("strategy", "A"),
                    spy_trend    = trade.get("spy_trend", "neutral"),
                    entry_reason = trade.get("reason", ""),
                )
                replayed += 1
            except Exception:
                continue
        if replayed > 0:
            log(f"🧠 Boot replay: {replayed} closed trades → AI memory seeded "
                f"({len(prompt_builder.memory.lessons)} lessons active)")
    except Exception as e:
        log(f"⚠️ Boot replay failed: {e}")

def get_trading_pool(equity):
    """Calculate fund pools — always protect growth reserve."""
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
    """Check and update autonomy tier based on equity growth."""
    try:
        current_tier = shared_state["autonomy_tier"]
        tiers = RULES["autonomy_tiers"]
        unlocked_tier = 0
        unlocked_data = None
        for i, tier in enumerate(tiers):
            if equity >= tier["equity"]:
                unlocked_tier = i + 1
                unlocked_data = tier
        if unlocked_tier > current_tier:
            tier_data       = tiers[unlocked_tier - 1]
            autonomous_fund = tier_data["autonomous_fund"]
            c_alloc = shared_state["claude_allocation"]
            g_alloc = shared_state["grok_allocation"]
            half_fund = autonomous_fund / 2
            c_fund = round(half_fund * (c_alloc / 0.5), 2)
            g_fund = round(half_fund * (g_alloc / 0.5), 2)
            if c_fund + g_fund > autonomous_fund:
                c_fund = round(autonomous_fund * c_alloc, 2)
                g_fund = round(autonomous_fund - c_fund, 2)
            collab_pool = round((equity * RULES["trading_pool_pct"]) - autonomous_fund, 2)
            shared_state["autonomy_tier"]    = unlocked_tier
            shared_state["autonomy_mode"]    = True
            shared_state["claude_auto_fund"] = c_fund
            shared_state["grok_auto_fund"]   = g_fund
            log(f"🎉 AUTONOMY TIER {unlocked_tier} UNLOCKED! — {tier_data['description']}")
            log(f"🔵 Claude autonomous fund: ${c_fund:.2f} ({c_alloc*100:.0f}%)")
            log(f"🔴 Grok autonomous fund:   ${g_fund:.2f} ({g_alloc*100:.0f}%)")
            log(f"🤝 Collaborative pool: ${collab_pool:.2f}")
            if unlocked_tier < len(tiers):
                next_t = tiers[unlocked_tier]
                needed = next_t["equity"] - equity
                log(f"🎯 Next tier: ${next_t['equity']} (need ${needed:.2f} more)")
            return True, tier_data
        return False, unlocked_data
    except Exception as e:
        log(f"⚠️ check_autonomy_tier: {e}")
        return False, None

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
    except Exception:
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

def update_gain_metrics(equity):
    """
    Update month and YTD equity baselines on rollover.
    Call once per cycle with current equity.
    """
    from datetime import datetime
    now         = datetime.now()
    cur_month   = now.strftime("%Y-%m")
    cur_year    = now.strftime("%Y")

    # Month rollover
    if shared_state.get("last_reset_month") != cur_month:
        if shared_state.get("last_reset_month") is not None:
            # Accumulate last month's gain before resetting
            prev_month_gain = equity - shared_state.get("month_start_equity", equity)
            shared_state["ytd_pnl"] = round(
                shared_state.get("ytd_pnl", 0) + prev_month_gain, 2)
        shared_state["month_start_equity"] = equity
        shared_state["month_pnl"]          = 0.0
        shared_state["last_reset_month"]   = cur_month
        # Reset crypto month
        shared_state["crypto_month_start"] = shared_state.get("crypto_day_start", 0)
        shared_state["crypto_month_pnl"]   = 0.0

    # Year rollover
    if shared_state.get("last_reset_year") != cur_year:
        shared_state["year_start_equity"]  = equity
        shared_state["ytd_pnl"]            = 0.0
        shared_state["last_reset_year"]    = cur_year
        shared_state["crypto_year_start"]  = shared_state.get("crypto_day_start", 0)
        shared_state["crypto_ytd_pnl"]     = 0.0

    # Live month P&L
    month_start = shared_state.get("month_start_equity", equity)
    if month_start and month_start > 0:
        shared_state["month_pnl"] = round(equity - month_start, 2)

    # Live YTD P&L (month_pnl + accumulated prior months)
    year_start = shared_state.get("year_start_equity", equity)
    if year_start and year_start > 0:
        shared_state["ytd_pnl"] = round(equity - year_start, 2)

def format_gains(equity, crypto_wallet_value=None):
    """
    Build a compact gains summary string for log output.
    Shows: Day | Week | Month | YTD for stocks.
    Appends crypto gains if wallet value provided.
    """
    day_start   = shared_state.get("day_start_equity",   equity)
    week_start  = shared_state.get("week_start_equity",  equity)
    month_start = shared_state.get("month_start_equity", equity)
    year_start  = shared_state.get("year_start_equity",  equity)

    def _fmt(pnl, start):
        pct = (pnl / start * 100) if start and start > 0 else 0
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⬜"
        return f"{icon} {pnl:+.2f} ({pct:+.1f}%)"

    day_gain   = equity - day_start   if day_start   > 0 else 0
    week_gain  = equity - week_start  if week_start  > 0 else 0
    month_gain = equity - month_start if month_start > 0 else 0
    ytd_gain   = equity - year_start  if year_start  > 0 else 0

    stock_line = (
        f"📈 Gains  Day: {_fmt(day_gain, day_start)}"
        f"  Week: {_fmt(week_gain, week_start)}"
        f"  Month: {_fmt(month_gain, month_start)}"
        f"  YTD: {_fmt(ytd_gain, year_start)}"
    )

    if crypto_wallet_value is not None:
        c_day   = crypto_wallet_value - shared_state.get("crypto_day_start",   crypto_wallet_value)
        c_week  = crypto_wallet_value - shared_state.get("crypto_week_start",  crypto_wallet_value)
        c_month = crypto_wallet_value - shared_state.get("crypto_month_start", crypto_wallet_value)
        c_ytd   = crypto_wallet_value - shared_state.get("crypto_year_start",  crypto_wallet_value)

        def _cfmt(pnl, start):
            pct  = (pnl / start * 100) if start and start > 0 else 0
            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⬜"
            return f"{icon} {pnl:+.2f} ({pct:+.1f}%)"

        crypto_line = (
            f"🪙 Crypto  Day: {_cfmt(c_day, shared_state.get('crypto_day_start', crypto_wallet_value))}"
            f"  Week: {_cfmt(c_week, shared_state.get('crypto_week_start', crypto_wallet_value))}"
            f"  Month: {_cfmt(c_month, shared_state.get('crypto_month_start', crypto_wallet_value))}"
            f"  YTD: {_cfmt(c_ytd, shared_state.get('crypto_year_start', crypto_wallet_value))}"
        )
        return stock_line + "\n" + crypto_line

    return stock_line

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
    """Return feature flags dict; safe defaults on failure."""
    try:
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
    except Exception as e:
        log(f"⚠️ check_account_features: {e}")
        return {"can_short": False, "until_short": 2000, "equity": 0, "shorting_enabled": False, "short_progress_pct": 0, "is_margin": False}

# ── Market Data ──────────────────────────────────────────
# [get_bars → moved to market_data.py / intelligence.py]
# [get_intraday_bars → moved to market_data.py / intelligence.py]
# [compute_intraday_indicators → moved to market_data.py / intelligence.py]
# [_compute_breakout → moved to market_data.py / intelligence.py]
# [compute_indicators → moved to market_data.py / intelligence.py]
# [get_chart_section → moved to market_data.py / intelligence.py]
