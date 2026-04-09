import os
import time
import json
import httpx
import requests
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── 5-Layer Projection Engine ────────────────────────────────
# projection_engine.py must be in the same directory (already in GitHub)
from projection_engine import (
    get_projection,
    score_buy_opportunity      as proj_score_buy,
    format_projection_for_ai   as proj_format_for_ai,
    get_position_exit_guidance as proj_get_exit_guidance,
)

# ── Adaptive Prompt Builder + Evolving Memory ─────────────────
# prompt_builder.py must be in the same directory (already in GitHub)
from prompt_builder import PromptBuilder
prompt_builder = PromptBuilder()   # Single instance — memory grows all session

# ── Binance.US Crypto Trading Engine ─────────────────────────
# binance_crypto.py must be in the same directory
from binance_crypto import CryptoTrader
crypto_trader = CryptoTrader()     # 24/7 crypto — runs parallel to stocks

# ── v3.0 AI-Led Architecture ──────────────────────────────────────
try:
    from thesis_manager import ThesisManager, build_sleep_brief_prompt, build_portfolio_analysis_prompt, parse_sleep_brief
    from wallet_intelligence import WalletIntelligence
    thesis_mgr   = ThesisManager()
    wallet_intel = WalletIntelligence()
except ImportError:
    thesis_mgr   = None
    wallet_intel = None

# ── Self-Repair Engine ────────────────────────────────────────
try:
    from self_repair import scan_log_line as _repair_scan, get_repair_status, reset_session as _repair_reset
    _REPAIR_ENABLED = True
except ImportError:
    _REPAIR_ENABLED = False
    def _repair_scan(x): pass
    def get_repair_status(): return {"configured": False, "error": "self_repair.py not found"}
    def _repair_reset(): pass

# ── Projection Accuracy Tracker ──────────────────────────────
# Defined here (not in projection_engine.py) because it writes to shared_state.
# Call from run_afterhours() to build a rolling accuracy score over time.
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
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

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
    "threshold_active_mult":   1.5,   # Was 1.2, then 1.1 — now 1.5 to reduce false wakes
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
    "month_start_equity":  55.0,
    "year_start_equity":   55.0,
    "month_pnl":           0.0,
    "ytd_pnl":             0.0,
    "last_reset_month":    None,   # "YYYY-MM" to detect month rollover
    "last_reset_year":     None,   # "YYYY"    to detect year rollover
    # Crypto performance tracking
    "crypto_day_pnl":      0.0,
    "crypto_week_pnl":     0.0,
    "crypto_month_pnl":    0.0,
    "crypto_ytd_pnl":      0.0,
    "crypto_day_start":    0.0,   # USDT wallet value at day start
    "crypto_week_start":   0.0,
    "crypto_month_start":  0.0,
    "crypto_year_start":   0.0,
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
    # ── Hourly trend scan storage ──────────────────────────
    "trend_scan_results":   [],      # Latest scan findings stored for AI review
    "trend_scan_time":      None,    # When last scan ran
    "trend_alerts":         [],      # High-priority alerts (may wake AIs)
    "last_equity":          55.0,    # Track equity changes
    "last_cash":            0.0,     # Track cash changes
    "deposit_detected":     False,   # Flag when new deposit found
    "deposit_amount":       0.0,     # How much was deposited
}