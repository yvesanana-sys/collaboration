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

# ── Environment variables ────────────────────────────────────
ALPACA_KEY     = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
GROK_KEY       = os.environ.get("GROK_KEY", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH  = os.environ.get("GITHUB_BRANCH", "main")
BASE_URL       = "https://api.alpaca.markets"
DATA_URL       = "https://data.alpaca.markets"
BOT_NAME       = "NovaTrade"
PORT           = int(os.environ.get("PORT", 8080))

app = Flask(__name__)
CORS(app)

# ── Global shared state ───────────────────────────────────────
# Single dict — populated with defaults here, updated each cycle,
# persisted to volume on sleep/wake
shared_state: dict = {
    # Positions
    "claude_positions":    [],
    "grok_positions":      [],
    "bearish_watchlist":   [],
    # Fund allocation
    "claude_allocation":   0.50,
    "grok_allocation":     0.50,
    "growth_reserve":      0.0,
    # Performance tracking
    "claude_daily_pnl":    0.0,
    "grok_daily_pnl":      0.0,
    "claude_weekly_pnl":   0.0,
    "grok_weekly_pnl":     0.0,
    "claude_total_pnl":    0.0,
    "grok_total_pnl":      0.0,
    "claude_win_days":     0,
    "grok_win_days":       0,
    # Equity tracking
    "last_equity":         55.0,
    "day_start_equity":    55.0,
    "week_start_equity":   55.0,
    "month_start_equity":  55.0,
    "year_start_equity":   55.0,
    # AI health
    "claude_healthy":      True,
    "grok_healthy":        True,
    "claude_credits_ok":   True,
    "grok_credits_ok":     True,
    "claude_fail_reason":  "",
    "grok_fail_reason":    "",
    "failover_mode":       False,
    # Sleep/wake
    "ai_sleeping":         False,
    "sleep_reason":        "",
    "wake_reason":         "",
    "last_sleep_time":     None,
    "ai_wake_instructions": [],
    "trading_brief":       "",
    "tomorrows_plan":      "",
    # PDT / trading state
    "day_trade_count":     0,
    "day_trade_dates":     [],
    "intraday_buys":       {},
    "pdt_last_reset_date": "",
    "stops_fired_today":   0,
    "restricted_positions": set(),
    "failed_sells":        {},
    # Projections cache
    "last_projections":    {},
    "last_proj_time":      "",
    "spy_cache":           None,
    "spy_trend":           "neutral",
    # Autonomy
    "autonomy_mode":       "PRIME",
    "autonomy_tier":       0,
    "claude_auto_fund":    0.0,
    "grok_auto_fund":      0.0,
    "claude_collab_fund":  0.0,
    "grok_collab_fund":    0.0,
    "watch_mode_active":   False,
    "failover_mode":       False,
    # Crypto
    "crypto_day_start":    0.0,
    "crypto_last_day":     "",
    "crypto_last_run":     None,
    "crypto_month_start":  0.0,
    "crypto_year_start":   0.0,
    # Collaboration cycle throttle
    "last_collab_cycle_time": None,
    "collab_cycle_interval": 300,
    # Misc
    "boot_time":           None,
    "last_cash":           0.0,
    "last_equity":         55.0,
    "last_sync":           "",
    "last_snapshot_time":  None,
    "last_liquidation":    "",
    "liquidation_result":  None,
    "next_buy_target":     None,
    "trading_brief":       "",
    "last_rebalance_day":  "",
    "last_rebalance_week": "",
    "last_reset_month":    "",
    "last_reset_year":     "",
    "proj_hit_count":      0,
    "proj_total_count":    0,
    "proj_accuracy_pct":   0.0,
    "position_exits":      {},
    "sleeping_strategies": {},
    # P&L tracking
    "month_pnl":           0.0,
    "ytd_pnl":             0.0,
    "crypto_month_pnl":    0.0,
    "crypto_ytd_pnl":      0.0,
    # Wake/trend tracking
    "last_wake_time":      None,
    "trend_alerts":        [],
    "trend_scan_results":  {},
    "deposit_detected":    False,
}

# RULES imported from portfolio_manager

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
import binance_crypto  # Full module import for binance_get function
from binance_crypto import CryptoTrader

# ── Extracted modules ─────────────────────────────────────────
from market_data import (
    get_bars, get_intraday_bars, compute_intraday_indicators,
    _compute_breakout, compute_indicators, get_chart_section,
    get_news_context, get_fear_greed_index, get_earnings_calendar,
    get_market_context, get_spy_trend, get_biggest_gainers,
    get_recent_ipos, get_market_mode,
)
import market_data as _market_data

from intelligence import (
    get_politician_trades, analyze_politician_signals,
    get_top_investor_portfolios, analyze_smart_money,
)
import intelligence as _intelligence

from github_deploy import (
    github_get_file_sha, github_push_file, github_push_all,
)
import github_deploy as _github_deploy

from ai_clients import (
    ask_claude, ask_grok, clean_json_str, _expand_r1_keys,
    parse_json, ask_with_retry, classify_ai_error,
    safe_ask_claude, safe_ask_grok, check_ai_health,
)
import ai_clients as _ai_clients

from sleep_manager import (
    ai_sleep, ai_wake, check_wake_conditions, check_ai_wake_instructions,
)
import sleep_manager as _sleep_manager

from portfolio_manager import (
    RULES,
    track_projection_accuracy,
    sync_binance_history, get_binance_history_stats,
    _load_binance_history,
    _load_trade_history, _save_trade_history, record_trade,
    _load_shared_state, _save_shared_state,
    _save_sleep_state, _load_sleep_state, _save_all_persistent_state,
    _trim_trade_history_to_6months, _replay_trade_history_into_memory,
    get_trading_pool, check_autonomy_tier, get_autonomy_status,
    rebalance_autonomy_funds, rebalance_allocations,
    update_gain_metrics, format_gains, track_pnl, check_account_features,
)
import portfolio_manager as _portfolio_manager

from pdt_manager import (
    record_intraday_buy, is_day_trade, get_stock_tier,
    reset_intraday_buys_if_new_day, check_pdt_safe,
    run_pdt_hold_council, _pdt_fallback_plan,
    check_pdt_hold_plans, get_pdt_decision, get_pdt_status,
)
import pdt_manager as _pdt_manager

# ── Core Reserve (long-term wealth compounder, walled off from tactical AIs) ──
try:
    import core_reserve
    HAVE_CORE_RESERVE = True
except ImportError:
    core_reserve = None
    HAVE_CORE_RESERVE = False

# ── AI Evolution Tier System (Pass A: foundation; Pass B: self-modify) ──
try:
    import ai_evolution
    HAVE_AI_EVOLUTION = True
except ImportError:
    ai_evolution = None
    HAVE_AI_EVOLUTION = False

# ── Strategic Brain (Phase A: plumbing only; Phase B: activation) ──
# Lives in strategic_brain.py. In Phase A, ENABLE_STRATEGIST=False so this
# is purely structural — the module loads, endpoints respond with state,
# but no API calls happen. This lets us verify integration is clean before
# turning the strategists on in Phase B.
try:
    import strategic_brain
    HAVE_STRATEGIC_BRAIN = True
except ImportError:
    strategic_brain = None
    HAVE_STRATEGIC_BRAIN = False

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
    from self_repair import (
        scan_log_line as _repair_scan,
        get_repair_status,
        reset_session    as _repair_reset,
        reset_escalation_state,
    )
    _REPAIR_ENABLED = True
except ImportError:
    _REPAIR_ENABLED = False
    def _repair_scan(x): pass
    def get_repair_status(): return {"configured": False, "error": "self_repair.py not found"}
    def _repair_reset(): pass
    def reset_escalation_state(): pass

# Claude Code trigger — optional, graceful fallback if not deployed
try:
    import claude_code_trigger as _cc_trigger
    _CC_TRIGGER_AVAILABLE = True
except ImportError:
    _cc_trigger = None
    _CC_TRIGGER_AVAILABLE = False

# ── Projection Accuracy Tracker ──────────────────────────────
# Defined here (not in projection_engine.py) because it writes to shared_state.
# Call from run_afterhours() to build a rolling accuracy score over time.
# [track_projection_accuracy → portfolio_manager.py]
# [shared_state persistence → portfolio_manager.py]
# ── Trade history global — loaded from volume on boot ────────
# Functions live in portfolio_manager.py — list lives here as global
trade_history: list = []   # Populated by _load_trade_history() below
def alpaca_get(path):
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    res = requests.get(BASE_URL + path, headers=headers)
    res.raise_for_status()
    return res.json()

# ── Reusable Alpaca fetch helpers ────────────────────────────────────────────
# Centralises error handling — call these instead of inline alpaca("GET",...)
def get_account():
    """Fetch Alpaca account dict; returns {} on error."""
    try:
        return alpaca("GET", "/v2/account")
    except Exception as e:
        log(f"⚠️ get_account: {e}")
        return {}

def get_positions():
    """Fetch open Alpaca positions list; returns [] on error."""
    try:
        return alpaca("GET", "/v2/positions")
    except Exception as e:
        log(f"⚠️ get_positions: {e}")
        return []

def get_equity_cash():
    """Return (equity_float, cash_float) from Alpaca account."""
    a = get_account()
    return round(float(a.get("equity", 0)), 2), round(float(a.get("cash", 0)), 2)

# ── Collaboration cycle throttle guard ──────────────────────────
def should_run_collab_cycle():
    """Check if enough time has passed since last collaboration cycle."""
    now = time.time()
    last = shared_state.get("last_collab_cycle_time")
    interval = shared_state.get("collab_cycle_interval", 300)  # 5 minutes default
    if last is None:
        shared_state["last_collab_cycle_time"] = now
        return True
    if (now - last) >= interval:
        shared_state["last_collab_cycle_time"] = now
        return True
    return False

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME})

@app.route("/storage")
def storage_check():
    """Check Railway volume — confirms trade history is persisting correctly."""
    import os
    results = {}
    for path in ["/data/trade_history.json", "./trade_history.json"]:
        try:
            exists = os.path.exists(path)
            if exists:
                size  = os.path.getsize(path)
                mtime = os.path.getmtime(path)
                from datetime import datetime
                modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                with open(path) as f:
                    import json as _json
                    data = _json.load(f)
                results[path] = {
                    "exists":        True,
                    "size_bytes":    size,
                    "trade_count":   len(data),
                    "last_modified": modified,
                    "last_trade":    data[-1].get("time_et") if data else None,
                }
            else:
                results[path] = {"exists": False, "reason": "file not created yet — no trades have closed"}
        except Exception as e:
            results[path] = {"exists": False, "error": str(e)}

    # Check /data directory itself
    try:
        data_dir = os.listdir("/data")
        results["_volume_contents"] = data_dir
        results["_volume_mounted"]  = True
    except:
        results["_volume_mounted"]  = False

    return jsonify(results)
