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
# thesis_manager.py  — per-position AI thesis + smart wake conditions
# wallet_intelligence.py — unified cross-portfolio opportunity scanner
from thesis_manager import (
    ThesisManager,
    build_sleep_brief_prompt,
    build_portfolio_analysis_prompt,
    parse_sleep_brief,
)
from wallet_intelligence import WalletIntelligence

thesis_mgr   = ThesisManager()      # Single instance — persists thesis to disk
wallet_intel = WalletIntelligence() # Single instance — reads full portfolio

# ── Self-Repair Engine ────────────────────────────────────────
# Monitors logs for recurring errors → calls Claude → opens GitHub PR
from self_repair import scan_log_line as _repair_scan, get_repair_status, reset_session as _repair_reset

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
    "threshold_active_mult":   1.1,   # Was 1.2 — easier to wake AIs
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
    # ── AI Sleep/Wake System ──
    "ai_sleeping":          False,   # True = both AIs asleep, bot runs autonomous
    "sleep_reason":         None,    # Why they went to sleep
    "wake_reason":          None,    # What woke them up
    "last_sleep_time":      None,
    "last_wake_time":       None,
    "stops_fired_today":    0,       # Count of stop-losses fired while sleeping
    "sleeping_strategies":  {},      # Full exit strategies stored before sleep
    "ai_notes":             {},      # AI notes per position for bot to follow
    # ── AI Custom Wake Instructions ───────────────────────────
    # AIs write these before sleeping — bot checks every cycle
    # Format: list of condition dicts the bot evaluates autonomously
    "ai_wake_instructions": [],      # [{type, symbol, threshold, reason, priority}]
    # ── v3.0 Thesis System ───────────────────────────────────────
    "thesis_wake_reason":   None,    # Why thesis condition triggered wake
    "thesis_wake_context":  None,    # Rich context string for AI on wake
    "portfolio_brief_json": None,    # Last AI sleep brief (raw dict)
    "last_wallet_snapshot": None,    # Last full wallet snapshot text
    "btc_price_1h_ago":     0.0,     # For BTC correlation / flash crash detection
    # ── 15-min snapshot ticker ────────────────────────────────
    "last_snapshot_time":   None,    # Last time the clean snapshot was printed
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
    # ── 5-Layer Projection Engine State ──────────────────────
    "last_projections":    {},      # {symbol: projection_dict} — updated each cycle
    "last_proj_time":      None,    # ISO timestamp of last projection run
    "proj_hit_count":      0,       # How many projections were accurate
    "proj_total_count":    0,       # Total projections tracked
    "proj_accuracy_pct":   0.0,     # Rolling accuracy %
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
    "failed_sells":         {},      # {symbol: fail_count} — tracks persistent sell failures
    # ── Timing ────────────────────────────────────────────────
    "boot_time":            datetime.now(timezone.utc),  # Pre-set so crypto loop never sees None
    "crypto_last_run":      None,    # Wall-clock time of last crypto AI cycle
    # ── PDT (Pattern Day Trader) Protection ───────────────────
    # Tracks intraday buys to prevent selling same stock same day
    # Accounts < $25k get flagged as PDT after 3 day-trades in 5 days
    "intraday_buys":        {},      # {symbol: buy_date} — cleared daily at midnight
    "day_trade_count":      0,       # How many day trades used today (max 3)
    "day_trade_dates":      [],      # Last 5 days of day-trade dates for rolling window
}

app = Flask(__name__)
CORS(app)

# ── Trade History Log ─────────────────────────────────────
# Persists in memory; last 500 trades kept
# Each entry: buy or sell with full context for performance review
trade_history = []

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

def alpaca_get(path):
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    res = requests.get(BASE_URL + path, headers=headers)
    res.raise_for_status()
    return res.json()

# ── Reusable Alpaca fetch helpers ────────────────────────────────────────────
# Centralises error handling — call these instead of inline alpaca("GET",...).
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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME})

@app.route("/repair_status")
def repair_status_endpoint():
    """Show self-repair engine status — errors detected, PRs opened, etc."""
    try:
        status = get_repair_status()
        # Read directly from os.environ — most reliable source of truth
        status["debug"] = {
            "GITHUB_TOKEN_set":  bool(os.environ.get("GITHUB_TOKEN", "")),
            "GITHUB_TOKEN_len":  len(os.environ.get("GITHUB_TOKEN", "")),
            "GITHUB_REPO":       os.environ.get("GITHUB_REPO", "") or "NOT SET",
            "ANTHROPIC_KEY_set": bool(os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")),
        }
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/pdt")
def pdt_status_endpoint():
    """Check PDT status and active hold plans."""
    try:
        account = alpaca("GET", "/v2/account")
        equity  = float(account.get("equity", 55))
        status  = get_pdt_status(equity)
        guidance = {}
        projections = shared_state.get("last_projections", {})
        for sym in status.get("intraday_buys", []):
            proj = projections.get(sym, {})
            if proj and not proj.get("error"):
                guidance[sym] = {
                    "bias":       proj.get("bias", "unknown"),
                    "confidence": proj.get("confidence", 0),
                    "proj_high":  proj.get("proj_high"),
                    "proj_low":   proj.get("proj_low"),
                    "recommendation": (
                        "HOLD OVERNIGHT — bullish projection"
                        if proj.get("bias") == "bullish"
                        else "CONSIDER SELLING — bearish projection"
                        if proj.get("bias") == "bearish"
                        else "HOLD — neutral, set tight stop"
                    ),
                }
        status["projection_guidance"] = guidance
        hold_plans = {k.replace("pdt_hold_", ""): v
                      for k, v in shared_state.items()
                      if k.startswith("pdt_hold_")}
        status["active_hold_plans"] = hold_plans
        status["explanation"] = (
            "PDT rule: accounts < $25,000 limited to 3 day trades per 5 business days."
        )
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/liquidate", methods=["GET", "POST"])
def liquidate_endpoint():
    """
    SPOT LIQUIDATION — sells free coins to USDT. Staked assets untouched.
    GET  /liquidate          → preview
    GET  /liquidate?confirm=yes → execute
    """
    try:
        from binance_crypto import (liquidate_all_to_usdt,
                                    get_full_wallet, get_staking_info)
    except Exception as ie:
        return jsonify({"error": f"import failed: {ie}"}), 500

    confirm = request.args.get("confirm", "").lower() == "yes" \
              or request.method == "POST"

    if not confirm:
        # Preview mode — show what would be sold, nothing executed
        try:
            wallet  = get_full_wallet()
            staking = get_staking_info()

            # Staked assets — show as PROTECTED
            staked_assets = {}
            for s in staking:
                if not s.get("error") and s.get("staked_qty", 0) > 0:
                    staked_assets[s["asset"]] = s

            # Spot coins that would be sold
            spot_to_sell = []
            spot_to_skip = []
            for h in wallet.get("positions", []):
                asset = h["asset"]
                if asset in staked_assets:
                    continue  # Staked — protected
                free = h.get("free", 0)
                val  = h.get("value_usdt", 0)
                if free > 0 and val >= 1.0:
                    spot_to_sell.append({
                        "asset":      asset,
                        "qty":        free,
                        "value_usdt": val,
                        "price":      h.get("price", 0),
                        "action":     "SELL → USDT (market order, instant)",
                    })
                elif free > 0:
                    spot_to_skip.append({
                        "asset":  asset,
                        "qty":    free,
                        "value":  val,
                        "reason": "dust < $1",
                    })

            total_spot   = sum(h["value_usdt"] for h in spot_to_sell)
            usdt_now     = wallet.get("usdt_free", 0)
            usdt_after   = round(usdt_now + total_spot, 2)
            total_staked = sum(s.get("staked_value", 0)
                               for s in staked_assets.values())

            staked_summary = [
                {
                    "asset":       a,
                    "staked_qty":  s["staked_qty"],
                    "value_usdt":  s.get("staked_value", 0),
                    "rewards":     s.get("rewards_pending", 0),
                    "unbond_days": s.get("unbonding_days", "?"),
                    "action":      "PROTECTED — earning APY, not touched",
                }
                for a, s in staked_assets.items()
            ]

            return jsonify({
                "status":          "PREVIEW — add ?confirm=yes to execute",
                "warning":         "Sells all free spot coins to USDT via market orders. Staked assets (FET/AUDIO/KAVA) are left untouched.",
                "usdt_now":        round(usdt_now, 2),
                "usdt_after_sale": usdt_after,
                "spot_to_sell":    spot_to_sell,
                "spot_to_skip":    spot_to_skip,
                "staked_protected": staked_summary,
                "staked_total_value": round(total_staked, 2),
                "tip":             "Claim staking rewards separately from Binance.US → Earn → Staking",
                "execute_url":     "/liquidate?confirm=yes",
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── EXECUTE liquidation ───────────────────────────────────
    log("🔴 LIQUIDATION REQUESTED via /liquidate endpoint")
    log("   Converting all crypto holdings to USDT for pure trading...")

    try:
        result = liquidate_all_to_usdt(log_fn=log)

        # Store liquidation timestamp so bot knows to trade fresh
        shared_state["last_liquidation"] = datetime.now(timezone.utc).isoformat()
        shared_state["liquidation_result"] = result

        return jsonify({
            "status":      "LIQUIDATION EXECUTED",
            "coins_sold":  result["sold"],
            "skipped":     result["skipped"],
            "usdt_gained": result["usdt_gained"],
            "usdt_final":  result["usdt_final"],
            "failures":    result["failed"],
            "note":        (
                "All free spot coins sold to USDT. "
                "Staked assets (FET/AUDIO/KAVA) untouched — still earning APY. "
                "Claim staking rewards from Binance.US → Earn → Staking for extra USDT."
            ),
        })
    except Exception as e:
        log(f"❌ Liquidation error: {e}")
        return jsonify({"error": str(e), "status": "FAILED"}), 500
    """
    Check PDT (Pattern Day Trader) status.
    Shows day trades used, remaining, intraday buys, and projection guidance.
    GET /pdt
    """
    try:
        account = alpaca("GET", "/v2/account")
        equity  = float(account.get("equity", 55))
        status  = get_pdt_status(equity)

        # Add projection-based guidance for each intraday buy
        guidance = {}
        projections = shared_state.get("last_projections", {})
        for sym in status.get("intraday_buys", []):
            proj = projections.get(sym, {})
            if proj and not proj.get("error"):
                guidance[sym] = {
                    "bias":       proj.get("bias", "unknown"),
                    "confidence": proj.get("confidence", 0),
                    "proj_high":  proj.get("proj_high"),
                    "proj_low":   proj.get("proj_low"),
                    "recommendation": (
                        "HOLD OVERNIGHT — bullish projection, protect with trail stop"
                        if proj.get("bias") == "bullish"
                        else "CONSIDER SELLING — bearish projection despite PDT cost"
                        if proj.get("bias") == "bearish"
                        else "HOLD — neutral, set tight stop"
                    ),
                }
        status["projection_guidance"] = guidance

        # Active hold plans
        hold_plans = {k.replace("pdt_hold_", ""): v
                      for k, v in shared_state.items()
                      if k.startswith("pdt_hold_")}
        status["active_hold_plans"] = hold_plans

        status["explanation"] = (
            "PDT rule: accounts < $25,000 limited to 3 day trades per 5 business days. "
            "Day trade = buying AND selling same stock same day. "
            "Violation = account restricted for 90 days."
        )
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
            "ai_wake_instructions": shared_state.get("ai_wake_instructions", []),
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

@app.route("/history")
def history():
    """
    Full trade history — last N trades (default 100, max 500).
    Query params:
      ?limit=50        — return last N trades
      ?symbol=NVDA     — filter by ticker
      ?action=sell     — filter by action type
      ?owner=claude    — filter by AI owner
    """
    try:
        limit   = min(int(request.args.get("limit", 100)), 500)
        symbol  = request.args.get("symbol", "").upper()
        action  = request.args.get("action", "").lower()
        owner   = request.args.get("owner", "").lower()

        trades = list(reversed(trade_history))  # newest first

        if symbol: trades = [t for t in trades if t.get("symbol") == symbol]
        if action: trades = [t for t in trades if t.get("action","").startswith(action)]
        if owner:  trades = [t for t in trades if t.get("owner") == owner]

        trades = trades[:limit]

        return jsonify({
            "count":  len(trades),
            "total_recorded": len(trade_history),
            "trades": trades,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/performance")
def performance():
    """
    Trading performance analytics derived from trade_history.
    Returns win rate, avg P&L, best/worst trades, per-symbol breakdown,
    per-AI breakdown, and trend of trade quality over time.
    """
    try:
        sells = [t for t in trade_history if t.get("pnl_usd") is not None]
        buys  = [t for t in trade_history if t.get("action") == "buy"]

        total_trades  = len(sells)
        wins          = [t for t in sells if t.get("pnl_usd", 0) > 0]
        losses        = [t for t in sells if t.get("pnl_usd", 0) <= 0]
        win_rate      = round(len(wins) / total_trades * 100, 1) if total_trades else 0
        total_pnl     = round(sum(t.get("pnl_usd", 0) for t in sells), 2)
        avg_win       = round(sum(t.get("pnl_usd", 0) for t in wins) / len(wins), 2) if wins else 0
        avg_loss      = round(sum(t.get("pnl_usd", 0) for t in losses) / len(losses), 2) if losses else 0
        profit_factor = round(abs(sum(t.get("pnl_usd",0) for t in wins)) /
                              abs(sum(t.get("pnl_usd",0) for t in losses)), 2) if losses and wins else None

        best_trade  = max(sells, key=lambda t: t.get("pnl_usd", 0), default=None)
        worst_trade = min(sells, key=lambda t: t.get("pnl_usd", 0), default=None)

        # Per-symbol breakdown
        sym_stats = {}
        for t in sells:
            sym = t.get("symbol","?")
            if sym not in sym_stats:
                sym_stats[sym] = {"trades": 0, "wins": 0, "total_pnl": 0.0,
                                  "avg_pnl_pct": [], "strategies": []}
            sym_stats[sym]["trades"]    += 1
            sym_stats[sym]["total_pnl"] += t.get("pnl_usd", 0)
            if t.get("pnl_usd", 0) > 0:
                sym_stats[sym]["wins"] += 1
            if t.get("pnl_pct") is not None:
                sym_stats[sym]["avg_pnl_pct"].append(t["pnl_pct"])
            if t.get("strategy"):
                sym_stats[sym]["strategies"].append(t["strategy"])

        symbol_summary = {}
        for sym, s in sym_stats.items():
            symbol_summary[sym] = {
                "trades":    s["trades"],
                "wins":      s["wins"],
                "win_rate":  round(s["wins"]/s["trades"]*100, 1) if s["trades"] else 0,
                "total_pnl": round(s["total_pnl"], 2),
                "avg_pnl_pct": round(sum(s["avg_pnl_pct"])/len(s["avg_pnl_pct"]), 2)
                               if s["avg_pnl_pct"] else None,
                "strategy_used": max(set(s["strategies"]), key=s["strategies"].count)
                                 if s["strategies"] else None,
            }

        # Per-AI breakdown
        ai_stats = {}
        for t in sells:
            owner = t.get("owner", "unknown")
            if owner not in ai_stats:
                ai_stats[owner] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
            ai_stats[owner]["trades"]    += 1
            ai_stats[owner]["total_pnl"] += t.get("pnl_usd", 0)
            if t.get("pnl_usd", 0) > 0:
                ai_stats[owner]["wins"] += 1

        ai_summary = {}
        for owner, s in ai_stats.items():
            ai_summary[owner] = {
                "trades":    s["trades"],
                "wins":      s["wins"],
                "win_rate":  round(s["wins"]/s["trades"]*100, 1) if s["trades"] else 0,
                "total_pnl": round(s["total_pnl"], 2),
            }

        # Exit reason breakdown
        reason_counts = {}
        for t in sells:
            r = t.get("action", "sell")
            reason_counts[r] = reason_counts.get(r, 0) + 1

        # Strategy A vs B performance
        strat_stats = {}
        for t in sells:
            s = t.get("strategy") or "unknown"
            if s not in strat_stats:
                strat_stats[s] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
            strat_stats[s]["trades"]    += 1
            strat_stats[s]["total_pnl"] += t.get("pnl_usd", 0)
            if t.get("pnl_usd", 0) > 0:
                strat_stats[s]["wins"] += 1

        strat_summary = {}
        for s, d in strat_stats.items():
            strat_summary[s] = {
                "trades":    d["trades"],
                "win_rate":  round(d["wins"]/d["trades"]*100, 1) if d["trades"] else 0,
                "total_pnl": round(d["total_pnl"], 2),
            }

        # SPY trend performance (were trades better in bull vs bear market?)
        spy_stats = {}
        for t in sells:
            trend = t.get("spy_trend", "neutral")
            if trend not in spy_stats:
                spy_stats[trend] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
            spy_stats[trend]["trades"]    += 1
            spy_stats[trend]["total_pnl"] += t.get("pnl_usd", 0)
            if t.get("pnl_usd", 0) > 0:
                spy_stats[trend]["wins"] += 1

        spy_summary = {
            k: {"trades": v["trades"],
                "win_rate": round(v["wins"]/v["trades"]*100,1) if v["trades"] else 0,
                "total_pnl": round(v["total_pnl"],2)}
            for k, v in spy_stats.items()
        }

        return jsonify({
            "summary": {
                "total_closed_trades": total_trades,
                "total_buys":          len(buys),
                "win_rate_pct":        win_rate,
                "total_pnl":           total_pnl,
                "avg_win":             avg_win,
                "avg_loss":            avg_loss,
                "profit_factor":       profit_factor,
            },
            "best_trade":      best_trade,
            "worst_trade":     worst_trade,
            "by_symbol":       symbol_summary,
            "by_ai":           ai_summary,
            "by_strategy":     strat_summary,
            "by_exit_reason":  reason_counts,
            "by_spy_trend":    spy_summary,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/projection")
def projection_endpoint():
    """
    Live daily range projections for all universe symbols (or a single symbol).
    Uses the 5-layer model from projection_engine.py.

    Query params:
      ?symbol=NVDA  — single symbol projection
      ?full=1       — include layer_details breakdown
    """
    try:
        symbol  = request.args.get("symbol","").upper()
        full    = request.args.get("full","0") == "1"
        symbols = [symbol] if symbol else RULES["universe"]
        results = {}

        for sym in symbols:
            try:
                bars = get_bars(sym)
                ind  = compute_indicators(bars)
                proj = get_projection(sym, bars, ind=ind)
                if not full:
                    proj.pop("layer_details", None)
                results[sym] = proj
            except Exception as e:
                results[sym] = {"symbol": sym, "error": str(e)}

        # Cache for bot autonomous use
        shared_state["last_projections"] = {k: v for k, v in results.items() if not v.get("error")}
        shared_state["last_proj_time"]   = datetime.now().isoformat()

        return jsonify({
            "projections":      results,
            "formatted_prompt": proj_format_for_ai(results, include_low_conf=True),
            "accuracy": {
                "hit_count":    shared_state["proj_hit_count"],
                "total_count":  shared_state["proj_total_count"],
                "accuracy_pct": shared_state["proj_accuracy_pct"],
            },
            "cached_at":    shared_state["last_proj_time"],
            "symbol_count": len(results),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/prompt_memory")
def prompt_memory_endpoint():
    """
    Live view of the adaptive prompt memory — lessons learned from closed trades.
    Shows win rates by situation mode, AI patterns, regime stats, recent lessons.
    GET /prompt_memory
    """
    try:
        return jsonify(prompt_builder.get_memory_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/crypto_status")
def crypto_status_endpoint():
    """
    Live Binance.US crypto trading status.
    Shows open positions, P&L, projections, recent trades, rules.
    GET /crypto_status
    """
    try:
        return jsonify(crypto_trader.get_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/deploy", methods=["GET", "POST"])
def deploy_endpoint():
    """
    Push all bot files to GitHub, triggering Railway auto-deploy.
    GET  /deploy          — show deploy status / setup instructions
    POST /deploy          — trigger immediate push to GitHub
    POST /deploy?msg=text — push with custom commit message

    Requires GITHUB_TOKEN + GITHUB_REPO env vars in Railway.
    """
    if request.method == "GET":
        configured = bool(GITHUB_TOKEN and GITHUB_REPO)

        # ?push=1 triggers deploy from browser — no POST needed
        if request.args.get("push") == "1" and configured:
            msg = request.args.get("msg", "NovaTrade auto-deploy via browser")
            def _do_deploy():
                github_push_all(commit_msg=msg)
            threading.Thread(target=_do_deploy, daemon=True).start()
            return jsonify({
                "status":  "deploying",
                "message": f"Pushing to {GITHUB_REPO}:{GITHUB_BRANCH}...",
                "note":    "Check Railway logs in ~30s",
            }), 202

        return jsonify({
            "configured":    configured,
            "repo":          GITHUB_REPO or "not set",
            "branch":        GITHUB_BRANCH,
            "files":         _DEPLOY_FILES,
            "deploy_url":    "Add ?push=1 to this URL to trigger deploy from browser",
            "setup_required": {} if configured else {
                "GITHUB_TOKEN": "Create at github.com/settings/tokens (repo scope)",
                "GITHUB_REPO":  "Your repo e.g. yvesanana-sys/collaboration",
            }
        })

    # POST — trigger deploy
    try:
        msg = None
        if request.is_json:
            msg = request.json.get("message")
        if not msg:
            msg = request.args.get("msg")

        # Run in background thread — never blocks or crashes the bot
        def _do_deploy():
            github_push_all(commit_msg=msg)

        t = threading.Thread(target=_do_deploy, daemon=True)
        t.start()

        return jsonify({
            "status":  "deploying",
            "message": f"Pushing to {GITHUB_REPO}:{GITHUB_BRANCH} in background...",
            "note":    "Check Railway logs in ~30s for result",
        }), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    # Feed every log line to the self-repair scanner
    try:
        _repair_scan(msg)
    except Exception:
        pass  # Never let repair scanner crash the bot

# ══════════════════════════════════════════════════════════════
# GITHUB AUTO-DEPLOY
# Bot can push updated files to GitHub, triggering Railway redeploy.
# Requires GITHUB_TOKEN + GITHUB_REPO env vars in Railway.
# ══════════════════════════════════════════════════════════════

_DEPLOY_FILES = [
    "bot_with_proxy.py",
    "binance_crypto.py",
    "projection_engine.py",
    "prompt_builder.py",
]

def github_get_file_sha(filename: str) -> str | None:
    """Get current SHA of a file in GitHub repo (needed to update it)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        res = requests.get(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }, params={"ref": GITHUB_BRANCH}, timeout=10)
        if res.ok:
            return res.json().get("sha")
        return None
    except Exception:
        return None

def github_push_file(filename: str, content: str, commit_msg: str) -> dict:
    """
    Push a single file to GitHub via the Contents API.
    Returns {"success": bool, "message": str}
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"success": False, "message": "GITHUB_TOKEN or GITHUB_REPO not set"}
    try:
        import base64
        sha     = github_get_file_sha(filename)
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        body    = {
            "message": commit_msg,
            "content": encoded,
            "branch":  GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha  # Required for updates (not new files)

        res = requests.put(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
        }, json=body, timeout=30)

        if res.ok:
            action = "updated" if sha else "created"
            return {"success": True, "message": f"{filename} {action} successfully"}
        else:
            return {"success": False, "message": f"GitHub API error {res.status_code}: {res.text[:200]}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

def github_push_all(commit_msg: str = None) -> dict:
    """
    Push all 4 bot files to GitHub.
    Reads each file from the running container's /app directory.
    Returns summary of what succeeded/failed.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {
            "success": False,
            "message": "GitHub not configured. Add GITHUB_TOKEN and GITHUB_REPO to Railway env vars.",
            "setup": {
                "GITHUB_TOKEN": "Personal Access Token with repo scope → github.com/settings/tokens",
                "GITHUB_REPO":  "Your repo in owner/repo format e.g. hanz/novatrade",
                "GITHUB_BRANCH": f"Branch to push to (currently: {GITHUB_BRANCH})",
            }
        }

    if not commit_msg:
        commit_msg = (f"NovaTrade auto-deploy {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                      f"— bot self-update")

    results  = {}
    success  = 0
    failed   = 0

    log(f"🚀 GitHub auto-deploy starting → {GITHUB_REPO}:{GITHUB_BRANCH}")

    for filename in _DEPLOY_FILES:
        # Try /app first (Railway), then current directory
        for path in [f"/app/{filename}", f"./{filename}", filename]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                result = github_push_file(filename, content, commit_msg)
                results[filename] = result

                if result["success"]:
                    log(f"   ✅ {filename} → {GITHUB_REPO}")
                    success += 1
                else:
                    log(f"   ❌ {filename}: {result['message']}")
                    failed += 1
                break  # Stop trying paths on first success

            except FileNotFoundError:
                continue
            except Exception as e:
                results[filename] = {"success": False, "message": str(e)}
                failed += 1
                log(f"   ❌ {filename}: {e}")
                break

    summary = (f"GitHub deploy: {success}/{len(_DEPLOY_FILES)} files pushed "
               f"to {GITHUB_REPO}:{GITHUB_BRANCH}")
    log(f"{'✅' if failed == 0 else '⚠️'} {summary}")

    if success > 0:
        log(f"   Railway will auto-deploy from GitHub shortly (~60s)")

    return {
        "success":  failed == 0,
        "pushed":   success,
        "failed":   failed,
        "files":    results,
        "repo":     GITHUB_REPO,
        "branch":   GITHUB_BRANCH,
        "message":  summary,
        "commit":   commit_msg,
    }

# ── Crypto symbol guard ───────────────────────────────────────
# Prevents any crypto pair from accidentally routing through Alpaca.
# MSTR, COIN are stocks — allowed. BTC/ETH direct pairs are not.
_CRYPTO_BASES = frozenset([
    "BTC","ETH","SOL","DOGE","AVAX","LINK","ADA","DOT",
    "BNB","XRP","MATIC","ATOM","NEAR","KAVA","ONE","XTZ",
])

def is_crypto_symbol(symbol: str) -> bool:
    """
    Returns True if symbol is a crypto trading pair (not a stock).
    These must ONLY be traded via Binance.US — never through Alpaca.
    """
    s = (symbol or "").upper().strip()
    if s.endswith("USDT") or s.endswith("BUSD"):
        return True
    if "/" in s:   # BTC/USD format
        base = s.split("/")[0]
        return base in _CRYPTO_BASES
    # Plain crypto base without pair suffix (e.g. "BTC" typed alone)
    if s in _CRYPTO_BASES and len(s) <= 5:
        return True
    return False

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

def get_intraday_bars(symbol, timeframe="5Min", hours=7):
    """
    Fetch intraday bars for VWAP, candlestick patterns and volume delta.
    Default: 5-minute bars for the last 7 hours (covers full trading day).
    Returns list of bars with keys: t, o, h, l, c, v
    """
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = (f"{DATA_URL}/v2/stocks/{symbol}/bars"
               f"?timeframe={timeframe}&start={start}&end={end}"
               f"&limit=200&feed=iex&adjustment=raw")
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            url2 = (f"{DATA_URL}/v2/stocks/{symbol}/bars"
                    f"?timeframe={timeframe}&start={start}&end={end}&limit=200")
            res = requests.get(url2, headers=headers, timeout=10)
        if res.ok:
            bars = res.json().get("bars", [])
            return bars if bars else []
        return []
    except Exception as e:
        return []

def compute_intraday_indicators(intraday_bars):
    """
    Compute intraday indicators from 5-min bars:
      - VWAP (Volume Weighted Average Price)
      - Volume delta proxy (green vs red candle volume)
      - Candlestick patterns (last 3 candles)
      - Intraday trend (above/below VWAP)
      - Support/resistance levels from today's range
    """
    if not intraday_bars or len(intraday_bars) < 3:
        return None

    # ── VWAP ─────────────────────────────────────────────────
    # VWAP = sum(typical_price * volume) / sum(volume)
    # Typical price = (H + L + C) / 3
    total_pv  = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"]
                    for b in intraday_bars)
    total_vol = sum(b["v"] for b in intraday_bars)
    vwap = round(total_pv / total_vol, 2) if total_vol > 0 else None

    # ── Volume delta (buy vs sell pressure proxy) ─────────────
    # Green candle (close > open) = buying pressure
    # Red candle (close < open)   = selling pressure
    buy_vol  = sum(b["v"] for b in intraday_bars if b["c"] >= b["o"])
    sell_vol = sum(b["v"] for b in intraday_bars if b["c"] <  b["o"])
    total_delta_vol = buy_vol + sell_vol
    buy_pct  = round(buy_vol  / total_delta_vol * 100, 1) if total_delta_vol > 0 else 50
    sell_pct = round(sell_vol / total_delta_vol * 100, 1) if total_delta_vol > 0 else 50
    vol_delta_bias = "BUYERS" if buy_pct > 60 else "SELLERS" if sell_pct > 60 else "NEUTRAL"

    # ── Volume spike detection (intraday) ─────────────────────
    avg_bar_vol  = total_delta_vol / len(intraday_bars) if intraday_bars else 0
    last_bar_vol = intraday_bars[-1]["v"] if intraday_bars else 0
    intraday_vol_ratio = round(last_bar_vol / avg_bar_vol, 1) if avg_bar_vol > 0 else 0

    # ── Candlestick pattern detection (last 3 candles) ────────
    patterns = []
    bars = intraday_bars[-6:]  # Last 6 bars for context

    def body(b):    return abs(b["c"] - b["o"])
    def upper(b):   return b["h"] - max(b["c"], b["o"])
    def lower(b):   return min(b["c"], b["o"]) - b["l"]
    def range_(b):  return b["h"] - b["l"]
    def is_bull(b): return b["c"] > b["o"]
    def is_bear(b): return b["c"] < b["o"]

    if len(bars) >= 2:
        c0 = bars[-1]   # Current (latest)
        c1 = bars[-2]   # Previous

        # ── Hammer / Hanging Man ──────────────────────────────
        # Long lower wick (>2x body), small body, tiny upper wick
        if (range_(c0) > 0 and body(c0) > 0 and
            lower(c0) >= body(c0) * 2 and
            upper(c0) <= body(c0) * 0.5):
            pattern = "HAMMER" if is_bull(c0) else "HANGING_MAN"
            patterns.append(f"{pattern}(bullish reversal signal)" if is_bull(c0)
                            else f"{pattern}(bearish warning)")

        # ── Shooting Star / Inverted Hammer ──────────────────
        # Long upper wick (>2x body), small body, tiny lower wick
        if (range_(c0) > 0 and body(c0) > 0 and
            upper(c0) >= body(c0) * 2 and
            lower(c0) <= body(c0) * 0.5):
            pattern = "SHOOTING_STAR" if is_bear(c0) else "INVERTED_HAMMER"
            patterns.append(f"{pattern}(bearish reversal)" if is_bear(c0)
                            else f"{pattern}(potential reversal)")

        # ── Doji (indecision) ────────────────────────────────
        if range_(c0) > 0 and body(c0) <= range_(c0) * 0.1:
            patterns.append("DOJI(indecision — watch next candle)")

        # ── Bullish Engulfing ────────────────────────────────
        if (is_bear(c1) and is_bull(c0) and
            c0["o"] <= c1["c"] and c0["c"] >= c1["o"]):
            patterns.append("BULLISH_ENGULFING(strong buy signal)")

        # ── Bearish Engulfing ────────────────────────────────
        if (is_bull(c1) and is_bear(c0) and
            c0["o"] >= c1["c"] and c0["c"] <= c1["o"]):
            patterns.append("BEARISH_ENGULFING(strong sell signal)")

        # ── Liquidity grab / shakeout ────────────────────────
        # Big wick down but closes back up near open (the 8am NVDA pattern)
        if (range_(c0) > 0 and
            lower(c0) >= range_(c0) * 0.5 and
            c0["c"] >= (c0["o"] + c0["l"]) / 2):
            patterns.append("LIQUIDITY_GRAB(wick-down recovery — bullish)")

        # ── Bearish wick grab (stop hunt up) ────────────────
        if (range_(c0) > 0 and
            upper(c0) >= range_(c0) * 0.5 and
            c0["c"] <= (c0["o"] + c0["h"]) / 2):
            patterns.append("STOP_HUNT_HIGH(wick-up reversal — bearish)")

    if len(bars) >= 3:
        c0, c1, c2 = bars[-1], bars[-2], bars[-3]

        # ── Three white soldiers (strong uptrend) ────────────
        if (is_bull(c0) and is_bull(c1) and is_bull(c2) and
            c0["c"] > c1["c"] > c2["c"] and
            c0["o"] > c1["o"] > c2["o"]):
            patterns.append("THREE_WHITE_SOLDIERS(strong bullish trend)")

        # ── Three black crows (strong downtrend) ─────────────
        if (is_bear(c0) and is_bear(c1) and is_bear(c2) and
            c0["c"] < c1["c"] < c2["c"] and
            c0["o"] < c1["o"] < c2["o"]):
            patterns.append("THREE_BLACK_CROWS(strong bearish trend)")

        # ── Morning star (bullish reversal) ──────────────────
        if (is_bear(c2) and body(c1) <= range_(c1) * 0.3 and
            is_bull(c0) and c0["c"] > (c2["o"] + c2["c"]) / 2):
            patterns.append("MORNING_STAR(bullish reversal — high confidence)")

        # ── Evening star (bearish reversal) ──────────────────
        if (is_bull(c2) and body(c1) <= range_(c1) * 0.3 and
            is_bear(c0) and c0["c"] < (c2["o"] + c2["c"]) / 2):
            patterns.append("EVENING_STAR(bearish reversal — high confidence)")

    # ── Intraday support / resistance ─────────────────────────
    today_high = max(b["h"] for b in intraday_bars)
    today_low  = min(b["l"] for b in intraday_bars)
    current    = intraday_bars[-1]["c"]
    vwap_pos   = ("ABOVE_VWAP" if vwap and current > vwap * 1.001
                  else "BELOW_VWAP" if vwap and current < vwap * 0.999
                  else "AT_VWAP")

    # ── OBV (On-Balance Volume) from intraday bars ────────────
    obv = 0
    obv_values = []
    for i, b in enumerate(intraday_bars):
        if i == 0:
            obv += b["v"]
        elif b["c"] > intraday_bars[i-1]["c"]:
            obv += b["v"]
        elif b["c"] < intraday_bars[i-1]["c"]:
            obv -= b["v"]
        obv_values.append(obv)

    # OBV trend: compare last 6 bars
    obv_trend = "RISING" if len(obv_values) >= 6 and obv_values[-1] > obv_values[-6] else \
                "FALLING" if len(obv_values) >= 6 and obv_values[-1] < obv_values[-6] else "FLAT"

    return {
        "vwap":             vwap,
        "vwap_position":    vwap_pos,
        "buy_vol_pct":      buy_pct,
        "sell_vol_pct":     sell_pct,
        "vol_delta_bias":   vol_delta_bias,
        "intraday_vol_ratio": intraday_vol_ratio,
        "patterns":         patterns,
        "today_high":       round(today_high, 2),
        "today_low":        round(today_low, 2),
        "obv_trend":        obv_trend,
        "bar_count":        len(intraday_bars),
    }

def _compute_breakout(bars: list, close: float, vol_ratio, rsi_v) -> dict:
    """
    Detect 20-period high/low breakout with volume confirmation.
    Returns breakout signal, momentum score, and direction.
    """
    periods = RULES["breakout_periods"]  # 20
    vol_min = RULES["vol_spike_multiplier"]  # 1.5x

    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    breakout_high = None
    breakout_low  = None
    is_breakout_up   = False
    is_breakout_down = False

    if len(highs) >= periods + 1:
        period_high  = max(highs[-periods-1:-1])
        period_low   = min(lows[-periods-1:-1])
        breakout_high = round(period_high, 2)
        breakout_low  = round(period_low, 2)
        is_breakout_up   = close > period_high
        is_breakout_down = close < period_low

    vol_spike = (vol_ratio or 0) >= vol_min

    # Combined signal
    breakout_signal = (
        "BULLISH_BREAKOUT"  if is_breakout_up   and vol_spike else
        "BEARISH_BREAKDOWN" if is_breakout_down and vol_spike else
        "BREAKOUT_NO_VOL"   if (is_breakout_up or is_breakout_down) else
        "NO_BREAKOUT"
    )

    # Momentum quality score 0-100
    momentum_score = 0
    if rsi_v:
        if RULES["rsi_momentum_min"] <= rsi_v <= 75:
            momentum_score += 30
        elif rsi_v <= RULES["rsi_oversold_max"]:
            momentum_score += 25
    if vol_spike:
        momentum_score += 35
    if is_breakout_up:
        momentum_score += 35

    return {
        "breakout_high":    breakout_high,
        "breakout_low":     breakout_low,
        "is_breakout_up":   is_breakout_up,
        "is_breakout_down": is_breakout_down,
        "breakout_signal":  breakout_signal,
        "vol_spike":        vol_spike,
        "momentum_score":   momentum_score,
    }


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

    # ── OBV (On-Balance Volume) — daily ──────────────────────
    # Rising OBV + rising price = healthy uptrend (volume confirms move)
    # Rising price + falling OBV = distribution (smart money selling)
    obv = 0
    obv_series = []
    for i, b in enumerate(bars):
        if i == 0:
            obv += b["v"]
        elif b["c"] > bars[i-1]["c"]:
            obv += b["v"]
        elif b["c"] < bars[i-1]["c"]:
            obv -= b["v"]
        obv_series.append(obv)
    obv_trend = ("RISING"  if len(obv_series) >= 10 and obv_series[-1] > obv_series[-10]
            else "FALLING" if len(obv_series) >= 10 and obv_series[-1] < obv_series[-10]
            else "FLAT")
    # OBV divergence: price up but OBV falling = bearish divergence
    price_trend = ("UP"   if len(closes) >= 10 and closes[-1] > closes[-10]
              else "DOWN" if len(closes) >= 10 and closes[-1] < closes[-10]
              else "FLAT")
    obv_divergence = None
    if price_trend == "UP"   and obv_trend == "FALLING": obv_divergence = "BEARISH"
    if price_trend == "DOWN" and obv_trend == "RISING":  obv_divergence = "BULLISH"

    return {"close":round(close,2),"rsi":rsi_v,"macd":macd,
            "sma20":round(sma20,2) if sma20 else None,
            "sma50":round(sma50,2) if sma50 else None,
            "ema9":round(ema9,2),"ema21":round(ema21,2),
            "bb_pct":bb_pct,"vol_ratio":vol_ratio,"mom_5d":mom_5d,
            "obv_trend":obv_trend,"obv_divergence":obv_divergence,
            # ── Breakout detection ────────────────────────────
            **_compute_breakout(bars, close, vol_ratio, rsi_v)}

def get_chart_section():
    """
    Enhanced chart section — adds 5-layer projection line for every symbol.
    Projections cached in shared_state for autonomous bot use (zero AI calls).
    """
    lines       = []
    projections = {}

    for sym in RULES["universe"]:
        bars = get_bars(sym)
        ind  = compute_indicators(bars)
        if not ind:
            lines.append(f"  {sym}: insufficient data")
            continue

        # Daily indicator line
        lines.append(
            f"  {sym}: ${ind['close']} RSI={ind['rsi']} MACD={ind['macd']} "
            f"SMA20={ind['sma20']} SMA50={ind['sma50']} EMA9={ind['ema9']} "
            f"EMA21={ind['ema21']} BB%={ind['bb_pct']} "
            f"Vol={ind['vol_ratio']} Mom5d={ind['mom_5d']}% "
            f"OBV={ind['obv_trend']}"
            + (f" ⚠️OBV_DIV={ind['obv_divergence']}" if ind.get('obv_divergence') else "")
        )

        # NEW: Intraday 5-min bars — VWAP, volume delta, candlestick patterns
        try:
            intraday = get_intraday_bars(sym, timeframe="5Min", hours=8)
            if intraday and len(intraday) >= 5:
                id_ind = compute_intraday_indicators(intraday)
                if id_ind:
                    # VWAP line
                    vwap_line = (f"    → INTRADAY: VWAP=${id_ind['vwap']} "
                                 f"[{id_ind['vwap_position']}] "
                                 f"H={id_ind['today_high']} L={id_ind['today_low']} "
                                 f"| Volume: {id_ind['vol_delta_bias']} "
                                 f"(buy={id_ind['buy_vol_pct']}% sell={id_ind['sell_vol_pct']}%) "
                                 f"OBV_intra={id_ind['obv_trend']}")
                    if id_ind['intraday_vol_ratio'] >= 2.0:
                        vwap_line += f" 🔥VOL_SPIKE={id_ind['intraday_vol_ratio']}x"
                    lines.append(vwap_line)

                    # Candlestick patterns
                    if id_ind['patterns']:
                        lines.append(f"    → PATTERNS: {' | '.join(id_ind['patterns'][:3])}")
        except Exception:
            pass  # Never break chart section for intraday failure

        # 5-layer projection
        try:
            proj = get_projection(sym, bars, ind=ind)
            projections[sym] = proj
            if not proj.get("error"):
                lines.append(
                    f"    → PROJ: High={proj['proj_high']} Low={proj['proj_low']} "
                    f"Pivot={proj['pivot']} ATR=${proj['atr']} "
                    f"Bias={proj['bias'].upper()} Conf={proj['confidence']}/100 "
                    f"| {proj['trade_action']}"
                )
        except Exception:
            pass

    # Cache projections in shared_state — bot reads this while AIs sleep
    shared_state["last_projections"] = projections
    shared_state["last_proj_time"]   = datetime.now().isoformat()

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



# ── Capitol Trades Watchlist ──────────────────────────────────
# High-signal politicians ranked by trading volume + tech focus.
# URL: https://www.capitoltrades.com/politicians/{ID}
# Add/remove politicians here — no other code changes needed.
CAPITOL_TRADES_POLITICIANS = {
    "P000197": {"name": "Nancy Pelosi",         "party": "D", "focus": "tech"},
    "M001243": {"name": "Dave McCormick",        "party": "R", "focus": "broad"},
    "F000110": {"name": "Cleo Fields",           "party": "D", "focus": "broad"},
    "F000472": {"name": "Scott Franklin",        "party": "R", "focus": "broad"},
    "M001236": {"name": "Tim Moore",             "party": "R", "focus": "broad"},
    "P000608": {"name": "Scott Peters",          "party": "D", "focus": "tech"},
    "B001292": {"name": "Don Beyer",             "party": "D", "focus": "broad"},
    "C001047": {"name": "Shelley Moore Capito",  "party": "R", "focus": "energy"},
    "H000273": {"name": "John Hickenlooper",     "party": "D", "focus": "tech"},
    "M001234": {"name": "Kelly Morrison",        "party": "D", "focus": "broad"},
}

# Tickers always worth tracking regardless of universe
ALWAYS_TRACK_TICKERS = {
    "NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "META", "GOOGL",
    "PLTR", "AMD", "AVGO", "TSM", "NFLX", "CRM", "ORCL",
}

def _parse_capitoltrades_page(html):
    """
    Parse a Capitol Trades politician page HTML into structured trade dicts.
    Returns list of trade dicts:
      {"politician", "party", "ticker", "action", "size", "traded_date", "filed_date", "filed_after_days"}
    """
    import re
    trades = []

    # Find the trade table rows — each row has: issuer | published | traded | filed_after | type | size
    # Pattern matches ticker symbols like NVDA:US, AAPL:US from the issuer column
    ticker_pattern  = re.compile(r'([A-Z]{1,5}):US')
    # Match buy/sell type
    type_pattern    = re.compile(r'\|\s*(buy|sell)\s*\|', re.IGNORECASE)
    # Match size ranges like 100K–250K, 1M–5M, 1K–15K
    size_pattern    = re.compile(r'(\d+[KkMm][\–\-]\d+[KkMm]|\d+[KkMm])')
    # Match dates like "16 Jan  2026" or "2026-01-16"
    date_pattern    = re.compile(r'(\d{1,2}\s+\w{3}\s+\d{4}|\d{4}-\d{2}-\d{2})')
    # Match "days  N" for filed_after
    days_pattern    = re.compile(r'days\s+(\d+)')

    # Split by table row delimiter and parse each
    rows = html.split('|')
    i = 0
    while i < len(rows):
        row = rows[i].strip()
        # Look for ticker in this segment
        ticker_match = ticker_pattern.search(row)
        if ticker_match:
            ticker = ticker_match.group(1)
            # Collect the next few cells
            window = " | ".join(rows[i:i+8])
            action_match = type_pattern.search(window)
            size_match   = size_pattern.search(window)
            dates        = date_pattern.findall(window)
            days_match   = days_pattern.search(window)

            if action_match:
                action       = action_match.group(1).lower()
                size         = size_match.group(0) if size_match else "unknown"
                traded_date  = dates[1] if len(dates) > 1 else (dates[0] if dates else "")
                filed_date   = dates[0] if dates else ""
                filed_days   = int(days_match.group(1)) if days_match else 999

                trades.append({
                    "ticker":           ticker,
                    "action":           action,
                    "size":             size,
                    "traded_date":      traded_date,
                    "filed_date":       filed_date,
                    "filed_after_days": filed_days,
                })
        i += 1

    return trades

def _fetch_politician_trades(pol_id, pol_info, cutoff_days=60):
    """
    Fetch and parse one politician's recent trades from Capitol Trades.
    Returns list of normalized trade dicts or [] on failure.
    cutoff_days: only return trades filed within this many days
    """
    url = f"https://www.capitoltrades.com/politicians/{pol_id}"
    try:
        headers = {
            "User-Agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/122.0.0.0 Safari/537.36"),
            "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language":  "en-US,en;q=0.9",
            "Accept-Encoding":  "gzip, deflate, br",
            "Connection":       "keep-alive",
            "Referer":          "https://www.capitoltrades.com/politicians",
            "Cache-Control":    "no-cache",
            "Pragma":           "no-cache",
            "Sec-Fetch-Dest":   "document",
            "Sec-Fetch-Mode":   "navigate",
            "Sec-Fetch-Site":   "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []

        raw_trades = _parse_capitoltrades_page(resp.text)

        result = []
        for t in raw_trades:
            # Filter by recency
            if t["filed_after_days"] > cutoff_days:
                continue
            result.append({
                "politician": pol_info["name"],
                "party":      pol_info["party"],
                "ticker":     t["ticker"],
                "action":     t["action"],
                "size":       t["size"],
                "filed":      t["filed_date"],
                "traded":     t["traded_date"],
                "days_lag":   t["filed_after_days"],
                "source":     "capitoltrades",
            })
        return result

    except Exception:
        return []

def _fetch_recent_trades_feed(cutoff_days=30):
    """
    Fetch the main /trades feed from Capitol Trades — catches ALL politicians,
    not just the ones in our watchlist. Returns trades filed recently across
    the entire congress that match our universe or always-track tickers.
    """
    url = "https://www.capitoltrades.com/trades"
    try:
        headers = {
            "User-Agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/122.0.0.0 Safari/537.36"),
            "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language":  "en-US,en;q=0.9",
            "Accept-Encoding":  "gzip, deflate, br",
            "Connection":       "keep-alive",
            "Referer":          "https://www.capitoltrades.com/",
            "Cache-Control":    "no-cache",
            "Pragma":           "no-cache",
            "Sec-Fetch-Dest":   "document",
            "Sec-Fetch-Mode":   "navigate",
            "Sec-Fetch-Site":   "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []

        import re
        trades = []
        # Parse politician name from links like /politicians/P000197
        pol_name_pattern = re.compile(
            r'\[([^\]]+)\]\(https://www\.capitoltrades\.com/politicians/([A-Z0-9]+)\)'
        )
        ticker_pattern = re.compile(r'([A-Z]{1,5}):US')
        type_pattern   = re.compile(r'\|\s*(buy|sell)\s*\|', re.IGNORECASE)
        size_pattern   = re.compile(r'(\d+[KkMm][\–\-]\d+[KkMm])')
        days_pattern   = re.compile(r'days\s+(\d+)')
        date_pattern   = re.compile(r'(\d{1,2}\s+\w{3}\s+\d{4})')

        rows = resp.text.split('\n')
        for row in rows:
            ticker_match = ticker_pattern.search(row)
            if not ticker_match:
                continue
            ticker = ticker_match.group(1)
            # Only process tickers we care about
            all_tickers = set(RULES["universe"]) | ALWAYS_TRACK_TICKERS
            if ticker not in all_tickers:
                continue

            action_match = type_pattern.search(row)
            if not action_match:
                continue

            days_match = days_pattern.search(row)
            filed_days = int(days_match.group(1)) if days_match else 999
            if filed_days > cutoff_days:
                continue

            size_match = size_pattern.search(row)
            date_match = date_pattern.search(row)
            pol_match  = pol_name_pattern.search(row)

            trades.append({
                "politician": pol_match.group(1) if pol_match else "Unknown",
                "party":      "?",
                "ticker":     ticker,
                "action":     action_match.group(1).lower(),
                "size":       size_match.group(0) if size_match else "unknown",
                "filed":      date_match.group(0) if date_match else "",
                "traded":     "",
                "days_lag":   filed_days,
                "source":     "capitoltrades_feed",
            })

        return trades

    except Exception:
        return []

def get_politician_trades():
    """Fetch congressional stock trades from Capitol Trades API."""
    all_trades = []
    sources_ok = []

    # ── Strategy 1: Main trades feed (all politicians, our tickers) ──
    try:
        feed_trades = _fetch_recent_trades_feed(cutoff_days=30)
        if feed_trades:
            all_trades.extend(feed_trades)
            sources_ok.append(f"feed:{len(feed_trades)}")
    except Exception as e:
        log(f"⚠️ Capitol Trades feed: {e}")

    # ── Strategy 2: Individual watchlist politicians ──────────────
    # Only fetch top 5 to keep startup time reasonable (parallel would be ideal)
    # Priority order: highest volume / most followed first
    priority_pols = list(CAPITOL_TRADES_POLITICIANS.items())[:5]
    for pol_id, pol_info in priority_pols:
        try:
            pol_trades = _fetch_politician_trades(pol_id, pol_info, cutoff_days=45)
            if pol_trades:
                all_trades.extend(pol_trades)
                sources_ok.append(f"{pol_info['name'].split()[1]}:{len(pol_trades)}")
            time.sleep(0.3)  # Polite rate limiting
        except Exception:
            pass

    # ── Deduplicate ───────────────────────────────────────────────
    seen = set()
    unique_trades = []
    for t in all_trades:
        key = (t["politician"], t["ticker"], t["action"], t.get("traded",""))
        if key not in seen:
            seen.add(key)
            unique_trades.append(t)

    # ── Filter to actionable tickers only ────────────────────────
    all_tickers = set(RULES["universe"]) | ALWAYS_TRACK_TICKERS
    actionable  = [t for t in unique_trades if t["ticker"] in all_tickers]

    if actionable:
        log(f"🏛️ Capitol Trades: {len(actionable)} trades found "
            f"({len(unique_trades)} total) | sources: {', '.join(sources_ok)}")

        # Build summary text
        lines = []
        for t in sorted(actionable, key=lambda x: x.get("days_lag", 99))[:15]:
            icon = "🟢" if t["action"] == "buy" else "🔴"
            lines.append(
                f"  {icon} [{t['party']}] {t['politician']}: "
                f"{t['action'].upper()} {t['ticker']} "
                f"{t['size']} (filed {t['filed']}, {t['days_lag']}d lag)"
            )

        # Count buy pressure on each ticker
        buy_counts = {}
        for t in actionable:
            if t["action"] == "buy":
                buy_counts[t["ticker"]] = buy_counts.get(t["ticker"], 0) + 1
        if buy_counts:
            top_buys = sorted(buy_counts.items(), key=lambda x: -x[1])[:5]
            log(f"🏛️ Top politician buys: {top_buys}")

        return "\n".join(lines), actionable

    # ── Fallback: Grok if Capitol Trades completely unreachable ──
    log("⚠️ Capitol Trades unreachable — falling back to Grok web search")
    try:
        universe_str = ", ".join(RULES["universe"])
        prompt = (f"Search for US politician stock trades filed last 30 days. "
                  f"Focus on: {universe_str}, NVDA, AAPL, MSFT, AMZN, TSLA, META, GOOGL. "
                  f'Return ONLY JSON: {{"trades": [{{"politician":"Name","party":"D/R",'
                  f'"ticker":"SYM","action":"buy/sell","size":"$1k-$15k","filed":"YYYY-MM-DD"}}],'
                  f'"summary":"brief"}}')
        raw    = ask_grok(prompt,
            "Financial research assistant. Search congressional trades. ONLY valid JSON.")
        result = parse_json(raw)
        if result and result.get("trades"):
            trades = result["trades"]
            lines  = [
                f"  [{t.get('party','?')}] {t.get('politician','?')}: "
                f"{t.get('action','?').upper()} {t.get('ticker','?')} "
                f"{t.get('size','')} ({t.get('filed','')})"
                for t in trades[:12]
            ]
            return "\n".join(lines), trades
    except Exception as e:
        log(f"⚠️ Grok fallback also failed: {e}")

    return "  No politician trade data available", []

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
    Fetch genuine recent IPOs using Alpaca's listed_at date field.
    Only returns stocks that actually listed within min_days–max_days ago.
    Filters out established stocks that happen to have limited bar history.
    Requirements: 500k+ avg volume, $5–$500 price, listed 30–180 days ago.
    """
    try:
        today   = datetime.now(timezone.utc).date()
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

        # Get all active tradable assets — includes listed_at field
        res = requests.get(
            f"{BASE_URL}/v2/assets?status=active&asset_class=us_equity",
            headers=headers, timeout=15
        )
        if not res.ok:
            return []

        assets = res.json()

        # ── Filter to genuine recent IPOs using listed_at ────────
        # listed_at is the actual exchange listing date — reliable signal
        ipo_candidates = []
        for a in assets:
            listed_at = a.get("listed_at", "")
            if not listed_at:
                continue
            try:
                listed_date = datetime.fromisoformat(
                    listed_at.replace("Z", "+00:00")
                ).date()
            except Exception:
                continue

            days_since = (today - listed_date).days

            # Must be within our IPO window
            if not (min_days <= days_since <= max_days):
                continue

            sym = a.get("symbol", "")
            # Skip warrants, rights, units, preferred shares
            if not sym or sym.endswith(("W", "R", "U", "P", "+")):
                continue
            # Skip longer symbols (typically ETFs/structured products)
            if len(sym) > 5:
                continue
            # Must be tradable + easy to borrow
            if not (a.get("tradable") and a.get("easy_to_borrow")):
                continue

            ipo_candidates.append({
                "symbol":    sym,
                "days_old":  days_since,
                "listed_at": listed_at,
            })

        if not ipo_candidates:
            return []

        log(f"🆕 Genuine IPO candidates (listed {min_days}–{max_days}d ago): "
            f"{len(ipo_candidates)} stocks — sampling for volume/momentum...")

        # ── Fetch bars to check volume + momentum ────────────────
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=max_days + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Sort by most recent IPO first (freshest momentum potential)
        ipo_candidates.sort(key=lambda x: x["days_old"])

        recent_ipos = []
        for cand in ipo_candidates:
            sym = cand["symbol"]
            try:
                url = (f"{DATA_URL}/v2/stocks/{sym}/bars"
                       f"?timeframe=1Day&start={start}&end={end}&limit=200&feed=iex")
                r   = requests.get(url, headers=headers, timeout=6)
                if not r.ok:
                    continue
                bars = r.json().get("bars", [])
                if len(bars) < 5:
                    continue

                last_price = bars[-1]["c"]
                avg_vol    = sum(b["v"] for b in bars) / len(bars)
                mom_5d     = round(
                    (bars[-1]["c"] - bars[-6]["c"]) / bars[-6]["c"] * 100, 2
                ) if len(bars) >= 6 else 0

                # Quality gates: liquid, priced reasonably
                if avg_vol < 500_000:
                    continue
                if not (5 <= last_price <= 500):
                    continue

                recent_ipos.append({
                    "symbol":    sym,
                    "days_old":  cand["days_old"],
                    "price":     last_price,
                    "avg_vol":   round(avg_vol),
                    "mom_5d":    mom_5d,
                    "listed_at": cand["listed_at"],
                })

                if len(recent_ipos) >= 10:
                    break

            except Exception:
                continue

        if recent_ipos:
            recent_ipos = sorted(recent_ipos, key=lambda x: -abs(x["mom_5d"]))
            ipo_summary = [(i["symbol"], f"{i['days_old']}d", f"{i['mom_5d']:+.1f}%") for i in recent_ipos]
            log(f"🆕 Recent IPOs confirmed ({len(recent_ipos)}): {ipo_summary}")

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

def min_profitable_exit(entry_price: float, fee_pct: float = 0.0003,
                         min_profit_pct: float = 0.005) -> float:
    """Calculate minimum sell price that covers fees and slippage."""
    return round(entry_price * (1 + fee_pct + min_profit_pct), 2)

# ── AI Calls ─────────────────────────────────────────────
def ask_claude(prompt, system="You are a trading AI. Respond with ONLY valid compact JSON. No markdown, no prose, no extra text.", max_tokens=1200):
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

def ask_grok(prompt, system="You are a trading AI. Respond with ONLY valid compact JSON. No markdown, no prose, no extra text.", max_tokens=1200):
    with httpx.Client(timeout=60) as http:
        res = http.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-4-1-fast-non-reasoning", "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}]},
        )
        if not res.is_success: raise Exception(f"{res.status_code}: {res.text}")
        return res.json()["choices"][0]["message"]["content"]

def clean_json_str(raw):
    import re
    # Remove markdown code fences (```json ... ``` or ``` ... ```)
    raw = re.sub(r'```(?:json)?\s*', '', raw).replace('```', '').strip()
    # Remove non-printable chars except whitespace
    raw = "".join(ch for ch in raw if ord(ch) >= 32 or ch in "\n\t")
    # Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # Find the outermost JSON object or array
    first_brace  = raw.find("{")
    first_bracket = raw.find("[")
    if first_brace == -1 and first_bracket == -1:
        return raw
    if first_brace == -1:
        first = first_bracket
    elif first_bracket == -1:
        first = first_brace
    else:
        first = min(first_brace, first_bracket)
    if first > 0:
        raw = raw[first:]
    last = max(raw.rfind("}"), raw.rfind("]"))
    if last != -1:
        raw = raw[:last+1]
    return raw.strip()

# Abbreviated key → full key mapping for compact R1 responses
_R1_KEY_MAP = {
    "sn": "strategy_name",
    "mt": "market_thesis",
    "pt": "proposed_trades",
    "cc": "collaborative_candidates",
    "bw": "bearish_watchlist",
    # Inside proposed_trades objects
    "a":  "action",
    "s":  "symbol",
    "n":  "notional_usd",
    "c":  "confidence",
    "f":  "flags",
    "r":  "rationale",
}

def _expand_r1_keys(obj):
    """Recursively expand abbreviated R1 keys to full names."""
    if isinstance(obj, dict):
        return {_R1_KEY_MAP.get(k, k): _expand_r1_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_r1_keys(i) for i in obj]
    return obj

def parse_json(raw):
    try:
        raw = clean_json_str(raw)
        s=raw.find("{"); e=raw.rfind("}")+1
        if s==-1 or e==0: return None
        json_str = raw[s:e]
        result = None
        try:
            result = json.loads(json_str)
        except json.JSONDecodeError:
            # Try trimming truncated response — remove last incomplete field
            last_comma = json_str.rfind(",")
            if last_comma > 0:
                try: result = json.loads(json_str[:last_comma]+"}")
                except: pass
            # Try closing unclosed string + object as last resort
            if result is None:
                try:
                    patched = json_str.rstrip().rstrip(',').rstrip('"') + '"}'  # close truncation
                    result = json.loads(patched)
                except: pass
        if result and isinstance(result, dict):
            result = _expand_r1_keys(result)
        return result
    except: return None

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
    """
    Returns (mode, sleep_interval_minutes).
    Modes: sleep | premarket | opening | prime | power_hour | afterhours

    Weekend/holiday aware — checks day of week first, then
    uses Alpaca clock for holidays (once per hour, cached).
    """
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    weekday = now_et.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    mins    = now_et.hour * 60 + now_et.minute

    # ── Weekend: always sleep ─────────────────────────────────
    if weekday >= 5:
        return "sleep", 60

    # ── Weekday time-based mode ───────────────────────────────
    if   mins < 510:               return "sleep",      60
    elif 510  <= mins < 570:       return "premarket",  20
    elif 570  <= mins < 630:       return "opening",     5
    elif 630  <= mins < 900:       return "prime",       5
    elif 900  <= mins < 960:       return "power_hour",  5
    elif 960  <= mins < 1020:      return "afterhours", 20
    else:                          return "sleep",      60

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
    # Read from new compact "flags" field — or legacy "signals" list for back-compat
    flags_raw   = trade_data.get("f") or trade_data.get("flags") or ""
    signals_raw = trade_data.get("signals", [])
    # Merge both into one lowercase string for keyword matching
    signals_str = (flags_raw + " " + " ".join(signals_raw)).lower()

    # Strategy B signals (trailing — let it run)
    b_signals = [
        ind and ind.get("mom_5d", 0) and abs(ind["mom_5d"]) > 3,  # Strong momentum
        "ipo"        in signals_str,   # IPO momentum
        "momentum"   in signals_str,   # Momentum play
        "breakout"   in signals_str,   # Breakout
        ind and ind.get("vol_ratio", 1) > 1.5,                    # High volume
    ]

    # Strategy A signals (fixed — take profit quickly)
    a_signals = [
        "news"       in signals_str,   # News-driven (can reverse fast)
        "politician" in signals_str,   # Politician signal
        "earnings"   in signals_str,   # Earnings play
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

def get_spy_trend():
    """
    Check SPY trend vs 50-day SMA.
    Returns: "bull" / "bear" / "neutral"
    Uses cached value if live fetch fails — never shows $0.00.
    """
    try:
        bars = get_bars("SPY", days=60)
        if not bars or len(bars) < 50:
            try:
                end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                start = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
                headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
                url = f"{DATA_URL}/v2/stocks/SPY/bars?timeframe=1Day&start={start}&end={end}&limit=60"
                res = requests.get(url, headers=headers, timeout=10)
                if res.ok:
                    bars = res.json().get("bars", [])
            except Exception:
                pass
        if bars and len(bars) >= 50:
            closes  = [b["c"] for b in bars]
            sma50   = sum(closes[-50:]) / 50
            current = closes[-1]
            change  = round((current - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else 0
            result  = ("bull"    if current > sma50 * 1.01
                       else "bear" if current < sma50 * 0.99
                       else "neutral")
            # Cache successful result
            shared_state["spy_cache"] = (result, current, sma50, change)
            return result, current, sma50, change
    except Exception as e:
        log(f"⚠️ SPY trend check failed: {e}")

    # Return cached value if available — never return $0
    if shared_state.get("spy_cache"):
        return shared_state["spy_cache"]
    return "neutral", 0, 0, 0

def record_intraday_buy(symbol: str):
    """Record a buy so we can detect if selling same day = day trade."""
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    shared_state["intraday_buys"][symbol] = today
    log(f"📋 PDT: recorded intraday buy → {symbol} on {today} "
        f"({shared_state['day_trade_count']}/3 day trades used)")


def is_day_trade(symbol: str) -> bool:
    """Check if selling this symbol today would be a day trade."""
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    return shared_state["intraday_buys"].get(symbol) == today


def get_stock_tier(equity: float) -> dict:
    """Get current stock trading tier based on equity."""
    for t in RULES["stock_tiers"]:
        if t["min_equity"] <= equity < t["max_equity"]:
            return t
    return RULES["stock_tiers"][-1]


def reset_intraday_buys_if_new_day():
    """
    Call at start of each trading day to reset PDT daily tracking.
    Resets day_trade_count daily, keeps 5-day rolling window.
    """
    today     = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    last_date = shared_state.get("pdt_last_reset_date")
    if last_date == today:
        return  # Already reset today

    existing = shared_state.get("intraday_buys", {})
    stale = [s for s, d in existing.items() if d != today]
    for s in stale:
        del shared_state["intraday_buys"][s]

    # Reset daily counter, keep rolling window
    shared_state["pdt_last_reset_date"] = today
    shared_state["day_trade_count"]     = 0
    if stale:
        log(f"📋 PDT: new day — cleared {len(stale)} intraday records, counter reset to 0/3")


def check_pdt_safe(symbol: str, equity: float = 55.0) -> tuple:
    """Check if selling symbol is PDT-safe. Returns (safe, reason)."""
    try:
        if equity >= 25000:
            return True, "equity >= $25k — PDT not applicable"
        if not is_day_trade(symbol):
            return True, "not a day trade (bought on a different day)"
        used = shared_state.get("day_trade_count", 0)
        if used >= 3:
            return False, (f"PDT LIMIT REACHED: {used}/3 day trades this week — "
                           f"cannot sell {symbol} today.")
        return True, f"day trade OK — {3 - used} of 3 remaining"
    except Exception as e:
        log(f"⚠️ check_pdt_safe: {e}")
        return True, "pdt_check_error — defaulting safe"


def run_pdt_hold_council(symbol: str, pos: dict,
                          ask_claude_fn, ask_grok_fn) -> dict:
    """Run PDT hold council - both AIs simulate holding vs selling."""
    try:
        entry_price   = float(pos.get("avg_entry_price", 0))
        current_price = float(pos.get("current_price", entry_price))
        qty           = float(pos.get("qty", 0))
        pnl_pct       = round((current_price - entry_price) / entry_price * 100, 2)
        pnl_usd       = round((current_price - entry_price) * qty, 2)
        used          = shared_state.get("day_trade_count", 0)

        log(f"🤝 PDT HOLD COUNCIL: {symbol} — both AIs simulating multi-day outlook...")
        log(f"   Entry=${entry_price} | Now=${current_price} | "
            f"P&L={pnl_pct:+.1f}% (${pnl_usd:+.2f}) | PDT {used}/3")

        # ── Build multi-day projection data ──────────────────
        bars = get_bars(symbol, days=60)
        ind  = compute_indicators(bars)

        # Day 1 projection (today's remaining range)
        proj_d1 = get_projection(symbol, bars, ind=ind,
                                 open_price=current_price)

        # Simulate Day 2 and Day 3 using trend momentum
        # We shift the anchor price by the daily expected move (ATR)
        atr         = proj_d1.get("atr", current_price * 0.02)
        trend_score = proj_d1.get("trend_score", 0.5)
        bias        = proj_d1.get("bias", "neutral")
        rsi         = ind.get("rsi", 50) if ind else 50
        macd        = ind.get("macd", 0) if ind else 0
        mom_5d      = ind.get("mom_5d", 0) if ind else 0

        # Direction multiplier from trend
        direction   = (trend_score - 0.5) * 2   # -1 to +1
        daily_drift = atr * direction * 0.4      # conservative estimate

        # Day-by-day simulation
        simulated = []
        anchor = current_price
        for day in range(1, 5):
            anchor_next  = round(anchor + daily_drift, 2)
            proj_high_d  = round(anchor_next + atr * 0.6, 2)
            proj_low_d   = round(anchor_next - atr * 0.6, 2)
            # Decay: momentum fades after day 2
            daily_drift *= 0.75
            simulated.append({
                "day":        day,
                "label":      f"+{day}d",
                "anchor":     anchor_next,
                "proj_high":  proj_high_d,
                "proj_low":   proj_low_d,
                "net_pct":    round((anchor_next - entry_price) / entry_price * 100, 2),
            })
            anchor = anchor_next

        # Find the plateau — day when price stops rising meaningfully
        plateau_day = 1
        for i, d in enumerate(simulated):
            if i == 0:
                continue
            prev_high = simulated[i-1]["proj_high"]
            gain      = (d["proj_high"] - prev_high) / prev_high * 100
            if gain < 0.3:   # Less than 0.3% expected additional gain
                plateau_day = i
                break
            plateau_day = i + 1

        plateau = simulated[min(plateau_day, len(simulated)-1)]

        # ── Build simulation prompt for both AIs ─────────────
        sim_rows = "\n".join(
            f"  Day +{d['day']}: est=${d['anchor']:.2f} "
            f"range=[${d['proj_low']:.2f}–${d['proj_high']:.2f}] "
            f"vs entry={d['net_pct']:+.1f}%"
            for d in simulated
        )

        prompt = f"""=== PDT HOLD COUNCIL — {symbol} ===
SITUATION: PDT rule blocks selling {symbol} today ({used}/3 day trades used).
We must decide: how long to hold, where to exit, and where to stop.

CURRENT POSITION:
  Entry: ${entry_price} | Now: ${current_price} | P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})
  Qty: {qty} shares | PDT day trades used: {used}/3

TECHNICAL STATE:
  Bias: {bias.upper()} | Trend score: {trend_score:.2f} | ATR: ${atr:.2f}
  RSI: {rsi} | MACD: {macd:.4f} | 5d momentum: {mom_5d:+.2f}%

MULTI-DAY PRICE SIMULATION (bot projection model):
{sim_rows}
  Estimated plateau: Day +{plateau_day} at ~${plateau['proj_high']:.2f}
  (momentum decays ~25% each day — exit before plateau for best price)

TASK — HOLD COUNCIL DECISION:
1. HOW LONG to hold? (1, 2, 3, or 4 days) — based on simulation above
2. TARGET EXIT PRICE — where to place limit sell order
3. STOP PRICE — where to cut loss if wrong (NEVER below entry × 0.96)
4. TRAIL: Should stop follow price up? (yes/no)
5. DAILY REASSESS: Under what conditions should the plan change?
   (e.g. "if price hits $X sell immediately" or "if RSI > 70 exit")

CRITICAL RULES:
- Exit target must be realistic based on simulation — no wishful thinking
- Stop must be BELOW current price (protecting downside)
- If both AIs disagree on days, take the LOWER number (conservative)
- If projection turns BEARISH at any point, exit immediately regardless of plan

Respond in JSON:
{{"hold_days": 2, "exit_target": 385.50, "stop_price": 378.00, "trail_stop": true,
  "confidence": 75, "rationale": "brief", "daily_triggers": {{"sell_if_above": 388.0, "sell_if_below": 379.0}},
  "reassess_note": "Exit early if RSI > 72 or MACD crosses negative"}}"""

        # ── Ask both AIs ──────────────────────────────────────
        log(f"   🔵 Claude running hold simulation...")
        log(f"   🔴 Grok running hold simulation...")

        claude_plan = None
        grok_plan   = None

        try:
            raw = ask_claude_fn(prompt,
                "You are Claude making a multi-day hold decision. "
                "Run the simulation carefully. ONLY valid JSON.")
            claude_plan = parse_json(raw)
            if claude_plan:
                log(f"   🔵 Claude: hold {claude_plan.get('hold_days')}d "
                    f"exit=${claude_plan.get('exit_target')} "
                    f"stop=${claude_plan.get('stop_price')} "
                    f"conf={claude_plan.get('confidence')}%")
        except Exception as e:
            log(f"   ⚠️ Claude hold council failed: {e}")

        try:
            raw = ask_grok_fn(prompt,
                "You are Grok making a multi-day hold decision with X/Twitter sentiment. "
                "Run the simulation carefully. ONLY valid JSON.")
            grok_plan = parse_json(raw)
            if grok_plan:
                log(f"   🔴 Grok: hold {grok_plan.get('hold_days')}d "
                    f"exit=${grok_plan.get('exit_target')} "
                    f"stop=${grok_plan.get('stop_price')} "
                    f"conf={grok_plan.get('confidence')}%")
        except Exception as e:
            log(f"   ⚠️ Grok hold council failed: {e}")

        # ── Collaborate — merge both plans ────────────────────
        if not claude_plan and not grok_plan:
            log(f"   ⚠️ Both AIs failed — using projection-based fallback")
            return _pdt_fallback_plan(symbol, entry_price, current_price,
                                      plateau, atr, simulated)

        if claude_plan and not grok_plan:
            agreed_plan = claude_plan
            log(f"   🔵 Claude solo — using Claude plan")
        elif grok_plan and not claude_plan:
            agreed_plan = grok_plan
            log(f"   🔴 Grok solo — using Grok plan")
        else:
            # Both responded — negotiate
            c_days   = claude_plan.get("hold_days", 2)
            g_days   = grok_plan.get("hold_days", 2)
            c_exit   = claude_plan.get("exit_target", current_price * 1.05)
            g_exit   = grok_plan.get("exit_target", current_price * 1.05)
            c_stop   = claude_plan.get("stop_price", entry_price * 0.96)
            g_stop   = grok_plan.get("stop_price", entry_price * 0.96)
            c_conf   = claude_plan.get("confidence", 60)
            g_conf   = grok_plan.get("confidence", 60)

            # Conservative: take lower hold days, average exit, higher stop
            agreed_days = min(c_days, g_days)
            agreed_exit = round((c_exit + g_exit) / 2, 2)
            agreed_stop = round(max(c_stop, g_stop), 2)   # Higher = tighter
            agreed_conf = round((c_conf + g_conf) / 2)
            agreed_trail= claude_plan.get("trail_stop", False) or \
                          grok_plan.get("trail_stop", False)

            # Merge daily triggers — take the MORE conservative
            c_trig = claude_plan.get("daily_triggers", {})
            g_trig = grok_plan.get("daily_triggers", {})
            triggers = {
                "sell_if_above": min(
                    c_trig.get("sell_if_above", 9999),
                    g_trig.get("sell_if_above", 9999)
                ),
                "sell_if_below": max(
                    c_trig.get("sell_if_below", 0),
                    g_trig.get("sell_if_below", 0)
                ),
            }

            agreed_plan = {
                "hold_days":     agreed_days,
                "exit_target":   agreed_exit,
                "stop_price":    agreed_stop,
                "trail_stop":    agreed_trail,
                "confidence":    agreed_conf,
                "daily_triggers": triggers,
                "reassess_note": claude_plan.get("reassess_note", ""),
                "rationale":     f"Claude:{c_days}d/${c_exit} + Grok:{g_days}d/${g_exit} → agreed",
                "claude_plan":   claude_plan,
                "grok_plan":     grok_plan,
            }

            log(f"   🤝 AGREED: hold {agreed_days}d | "
                f"exit=${agreed_exit} | stop=${agreed_stop} | conf={agreed_conf}%")
            if triggers["sell_if_above"] < 9999:
                log(f"   📈 Trigger: sell immediately if price > ${triggers['sell_if_above']}")
            if triggers["sell_if_below"] > 0:
                log(f"   📉 Trigger: sell immediately if price < ${triggers['sell_if_below']}")

        # ── Store the plan ────────────────────────────────────
        now_et   = datetime.now(ZoneInfo("America/New_York"))
        plan_key = f"pdt_hold_{symbol}"
        agreed_plan.update({
            "symbol":        symbol,
            "entry_price":   entry_price,
            "price_at_plan": current_price,
            "pnl_at_plan":   pnl_pct,
            "plan_date":     now_et.date().isoformat(),
            "plan_time":     now_et.strftime("%H:%M ET"),
            "expires_days":  agreed_plan.get("hold_days", 2),
            "simulation":    simulated,
            "plateau_day":   plateau_day,
            "plateau_price": plateau["proj_high"],
        })
        shared_state[plan_key] = agreed_plan
        log(f"   ✅ Hold plan stored: exit ${agreed_plan['exit_target']} "
            f"in {agreed_plan['hold_days']} day(s) | "
            f"plateau est. ${plateau['proj_high']:.2f} on day +{plateau_day}")

        return agreed_plan

    except Exception as e:
        log(f"   ❌ Hold council error: {e}")
        return {}


def _pdt_fallback_plan(symbol, entry_price, current_price,
                       plateau, atr, simulated) -> dict:
    """Projection-only fallback when both AIs fail."""
    stop  = round(max(entry_price * 0.96, current_price - 1.5 * atr), 2)
    return {
        "hold_days":     min(plateau["day"], 2),
        "exit_target":   plateau["proj_high"],
        "stop_price":    stop,
        "trail_stop":    True,
        "confidence":    55,
        "rationale":     "Projection-based fallback (AIs unavailable)",
        "simulation":    simulated,
        "plateau_day":   plateau["day"],
        "plateau_price": plateau["proj_high"],
    }


def check_pdt_hold_plans():
    """
    Called every 5-min tick. Checks all active hold plans:
    - Has price hit the exit target? → sell now
    - Has price hit stop? → cut loss
    - Has price surged (>5%)? → re-run hold council with new data
    - Is plan expired (hold_days elapsed)? → sell at market
    Called from the autonomous bot loop (no AI needed for basic checks).
    """
    plans_to_check = {k: v for k, v in shared_state.items()
                      if k.startswith("pdt_hold_")}
    if not plans_to_check:
        return

    try:
        positions  = {p["symbol"]: p for p in alpaca("GET", "/v2/positions")}
        account    = alpaca("GET", "/v2/account")
        equity     = float(account.get("equity", 55))
        now_et     = datetime.now(ZoneInfo("America/New_York"))
        today      = now_et.date().isoformat()
    except Exception:
        return

    for plan_key, plan in list(plans_to_check.items()):
        symbol    = plan.get("symbol", "")
        pos       = positions.get(symbol)
        if not pos:
            # Position no longer exists — clean up plan
            del shared_state[plan_key]
            continue

        curr      = float(pos.get("current_price", 0))
        entry     = plan.get("entry_price", 0)
        exit_tgt  = plan.get("exit_target", 0)
        stop_px   = plan.get("stop_price", 0)
        hold_days = plan.get("hold_days", 2)
        plan_date = plan.get("plan_date", today)
        triggers  = plan.get("daily_triggers", {})
        pnl_pct   = round((curr - entry) / entry * 100, 2) if entry else 0

        # Days elapsed since plan was made
        try:
            days_elapsed = (now_et.date() -
                __import__("datetime").date.fromisoformat(plan_date)).days
        except Exception:
            days_elapsed = 0

        action = None
        reason = ""

        # ── Check all exit conditions ─────────────────────────
        if curr >= exit_tgt and exit_tgt > 0:
            action = "sell"
            reason = f"🎯 PDT hold plan: exit target ${exit_tgt} HIT at ${curr:.2f} (+{pnl_pct:.1f}%)"

        elif curr <= stop_px and stop_px > 0:
            action = "sell"
            reason = f"🛑 PDT hold plan: stop ${stop_px} hit at ${curr:.2f} ({pnl_pct:.1f}%)"

        elif triggers.get("sell_if_above", 0) and curr >= triggers["sell_if_above"]:
            action = "sell"
            reason = f"📈 PDT trigger: price ${curr:.2f} above trigger ${triggers['sell_if_above']}"

        elif triggers.get("sell_if_below", 0) and curr <= triggers["sell_if_below"]:
            action = "sell"
            reason = f"📉 PDT trigger: price ${curr:.2f} below trigger ${triggers['sell_if_below']}"

        elif days_elapsed >= hold_days:
            action = "sell"
            reason = f"⏰ PDT hold plan expired: {days_elapsed} days elapsed (planned {hold_days}d)"

        # ── Surge detection: re-run council if price jumped >5% ──
        price_at_plan = plan.get("price_at_plan", entry)
        surge_pct     = (curr - price_at_plan) / price_at_plan * 100 if price_at_plan else 0
        if surge_pct >= 5.0 and action != "sell":
            log(f"📊 PDT SURGE: {symbol} +{surge_pct:.1f}% since plan — "
                f"flagging for AI reassessment next cycle")
            shared_state[plan_key]["needs_reassess"] = True
            shared_state[plan_key]["price_at_plan"]  = curr  # Reset baseline

        # ── Trail stop update ─────────────────────────────────
        if plan.get("trail_stop") and action != "sell" and curr > stop_px:
            atr_val   = shared_state.get("last_projections", {}).get(
                symbol, {}).get("atr", curr * 0.02)
            new_stop  = round(curr - 1.0 * atr_val, 2)
            if new_stop > stop_px:
                shared_state[plan_key]["stop_price"] = new_stop
                log(f"🔼 PDT trail stop: {symbol} ${stop_px} → ${new_stop} "
                    f"(curr=${curr:.2f} +{pnl_pct:.1f}%)")

        if action == "sell":
            log(f"🤖 PDT AUTO-EXIT: {reason}")
            try:
                smart_sell(symbol, reason, pos)
                del shared_state[plan_key]
                log(f"   ✅ PDT plan closed for {symbol}")
            except Exception as e:
                log(f"   ❌ PDT auto-exit failed: {e}")


def get_pdt_decision(symbol: str, equity: float,
                     current_price: float, entry_price: float,
                     projections: dict) -> dict:
    """Get PDT-safe sell decision for a position."""
    used      = shared_state.get("day_trade_count", 0)
    remaining = max(0, 3 - used)
    pdt_safe, pdt_reason = check_pdt_safe(symbol, equity)

    proj          = projections.get(symbol, {})
    proj_bias     = proj.get("bias", "neutral") if proj else "neutral"
    proj_high     = proj.get("proj_high", 0) if proj else 0
    proj_low      = proj.get("proj_low", 0)  if proj else 0
    proj_conf     = proj.get("confidence", 0) if proj else 0
    atr           = proj.get("atr", 0)       if proj else 0

    pnl_pct       = round((current_price - entry_price) / entry_price * 100, 2)
    is_profitable = current_price > entry_price

    # ── If PDT safe, just sell normally ──────────────────────
    if pdt_safe:
        return {
            "action":            "sell",
            "reason":            pdt_reason,
            "pdt_used":          used,
            "pdt_left":          remaining,
            "proj_bias":         proj_bias,
            "new_stop":          None,
        }

    # ── PDT would be violated — use projection to decide ─────
    # Priority 1: If losing money AND projection is bearish → sell anyway
    # (losing money now + bearish tomorrow = must cut loss)
    if not is_profitable and proj_bias == "bearish" and proj_conf >= 50:
        return {
            "action":   "sell",
            "reason":   (f"OVERRIDE: PDT limit hit but {symbol} is losing "
                         f"({pnl_pct:+.1f}%) AND projection bearish (conf={proj_conf}) "
                         f"— cutting loss now is better than worse loss tomorrow"),
            "pdt_used": used,
            "pdt_left": remaining,
            "proj_bias": proj_bias,
            "new_stop":  None,
            "override":  True,
        }

    # Priority 2: If profitable AND projection bullish → hold overnight
    if is_profitable and proj_bias in ("bullish", "neutral") and proj_high > current_price:
        upside = round((proj_high - current_price) / current_price * 100, 1)
        # Set trail stop to lock in 50% of current profit
        trail_stop = round(entry_price + (current_price - entry_price) * 0.5, 2)
        if atr > 0:
            trail_stop = round(current_price - 1.0 * atr, 2)  # 1×ATR trail

        return {
            "action":            "hold_overnight",
            "reason":            (f"PDT limit — holding overnight: {symbol} +{pnl_pct:.1f}% "
                                  f"proj={proj_bias} high=${proj_high} (+{upside}% upside) "
                                  f"→ trail stop set to ${trail_stop}"),
            "pdt_used":          used,
            "pdt_left":          remaining,
            "proj_bias":         proj_bias,
            "proj_high":         proj_high,
            "proj_low":          proj_low,
            "new_stop":          trail_stop,
            "expected_tomorrow": f"${proj_low}–${proj_high}",
        }

    # Priority 3: Losing but projection neutral/bullish → hold, tighten stop
    if not is_profitable and proj_bias == "bullish":
        tight_stop = round(current_price * 0.98, 2)  # -2% from here
        return {
            "action":   "hold_overnight",
            "reason":   (f"PDT limit — holding: {symbol} {pnl_pct:+.1f}% but "
                         f"projection bullish (conf={proj_conf}) — may recover tomorrow. "
                         f"Tight stop at ${tight_stop}"),
            "pdt_used": used,
            "pdt_left": remaining,
            "proj_bias": proj_bias,
            "new_stop":  tight_stop,
        }

    # Priority 4: Ambiguous — hold with breakeven stop
    breakeven_stop = round(entry_price * 1.002, 2)  # tiny above entry
    return {
        "action":   "hold_overnight",
        "reason":   (f"PDT limit — holding overnight: {symbol} {pnl_pct:+.1f}% "
                     f"proj={proj_bias} — set stop to ${breakeven_stop} to protect entry"),
        "pdt_used": used,
        "pdt_left": remaining,
        "proj_bias": proj_bias,
        "new_stop":  breakeven_stop,
    }


def get_pdt_status(equity: float) -> dict:
    """Return current PDT status for /stats endpoint."""
    used     = shared_state.get("day_trade_count", 0)
    intraday = shared_state.get("intraday_buys", {})
    return {
        "equity":          round(equity, 2),
        "pdt_applies":     equity < 25000,
        "day_trades_used": used,
        "day_trades_left": max(0, 3 - used),
        "intraday_buys":   list(intraday.keys()),
        "warning": (f"⚠️ {used}/3 day trades used — 1 left, be careful!"
                    if used == 2 and equity < 25000
                    else f"🛑 PDT LIMIT REACHED — no more day trades today!"
                    if used >= 3 and equity < 25000
                    else None),
    }


def smart_sell(symbol, reason, pos):
    """Execute a smart limit sell, fall back to market order.
    Checks PDT rule + uses projections to decide hold-overnight vs sell."""
    # ── PDT projection-based decision ────────────────────────
    try:
        account       = alpaca("GET", "/v2/account")
        equity        = float(account.get("equity", 55))
        current_price = float(pos.get("current_price", 0)) or \
                        float(pos.get("avg_entry_price", 0))
        entry_price   = float(pos.get("avg_entry_price", 0))
        projections   = shared_state.get("last_projections", {})

        pdt = get_pdt_decision(symbol, equity, current_price,
                               entry_price, projections)

        if pdt["action"] == "hold_overnight":
            log(f"🌙 PDT HOLD: {pdt['reason']}")
            log(f"   Day trades: {pdt['pdt_used']}/3 used | "
                f"Proj: {pdt['proj_bias'].upper()} | "
                f"Tomorrow: {pdt.get('expected_tomorrow', 'N/A')}")

            # Run hold council if not already planned for this symbol
            plan_key = f"pdt_hold_{symbol}"
            if plan_key not in shared_state:
                log(f"   🤝 Triggering PDT hold council for {symbol}...")
                # Council runs in background — AIs will be called
                shared_state[f"pdt_council_pending_{symbol}"] = {
                    "symbol": symbol, "pos": pos, "reason": reason
                }
            else:
                existing = shared_state[plan_key]
                log(f"   📋 Existing hold plan: exit=${existing.get('exit_target')} "
                    f"in {existing.get('hold_days')}d | "
                    f"stop=${existing.get('stop_price')}")

            # Update stop if tighter
            if pdt.get("new_stop") and symbol in shared_state.get("position_exits", {}):
                old_stop = shared_state["position_exits"][symbol].get("stop_price", 0)
                new_stop = pdt["new_stop"]
                if new_stop > old_stop:
                    shared_state["position_exits"][symbol]["stop_price"] = new_stop
                    log(f"   🛡️ Trail stop updated: ${old_stop} → ${new_stop}")
            return False

        # Override: sell despite PDT (losing + bearish)
        if pdt.get("override"):
            log(f"⚠️ PDT OVERRIDE: {pdt['reason']}")

        # PDT-safe or override → count it if it's a day trade
        if is_day_trade(symbol):
            used = shared_state.get("day_trade_count", 0)
            shared_state["day_trade_count"] = used + 1
            today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            shared_state.setdefault("day_trade_dates", []).append(today)
            shared_state["day_trade_dates"] = shared_state["day_trade_dates"][-5:]
            log(f"📋 PDT: day trade #{used+1}/3 — {symbol}")

    except Exception as e:
        log(f"⚠️ PDT check error: {e} — proceeding with sell")

    # ── Multi-method sell — tries 4 methods before giving up ────
    # Method order: limit → market DELETE → close_position → notional market
    # Each method handles different Alpaca restrictions (fractional, halted, etc.)
    last_err = None

    # Method 1: Limit sell at mid-price (best fills, avoids market impact)
    try:
        snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
        headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        snap_res = requests.get(snap_url, headers=headers, timeout=5)
        if snap_res.ok:
            quote     = snap_res.json().get("quote", {})
            bid_price = float(quote.get("bp", 0))
            ask_price = float(quote.get("ap", 0))
            if bid_price > 0 and ask_price > 0:
                sell_price = round((bid_price + ask_price) / 2, 2)
                qty        = pos["qty"]
                alpaca("POST", "/v2/orders", {
                    "symbol": symbol, "qty": str(qty),
                    "side": "sell", "type": "limit",
                    "limit_price": str(sell_price),
                    "time_in_force": "day",
                })
                log(f"✅ LIMIT SELL {symbol} {qty} @ ${sell_price} — {reason}")
                shared_state.get("failed_sells", {}).pop(symbol, None)
                return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 1 (limit) failed: {last_err[:60]}")

    # Method 2: Market sell via DELETE /v2/positions (standard path)
    try:
        alpaca("DELETE", f"/v2/positions/{symbol}")
        log(f"✅ MARKET SELL {symbol} (DELETE positions) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 2 (DELETE) failed: {last_err[:60]}")

    # Method 3: Market order via POST /v2/orders (works when DELETE is restricted)
    try:
        qty = pos.get("qty", "1")
        alpaca("POST", "/v2/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        })
        log(f"✅ MARKET SELL {symbol} (POST orders) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 3 (POST market) failed: {last_err[:60]}")

    # Method 4: Notional sell — sell by dollar amount (handles fractional restrictions)
    try:
        market_val = float(pos.get("market_value", 0))
        if market_val > 0:
            alpaca("POST", "/v2/orders", {
                "symbol": symbol,
                "notional": str(round(market_val, 2)),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
            })
            log(f"✅ NOTIONAL SELL {symbol} ${market_val:.2f} (fractional fix) — {reason}")
            shared_state.get("failed_sells", {}).pop(symbol, None)
            return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 4 (notional) failed: {last_err[:60]}")

    # All 4 methods failed
    log(f"❌ ALL SELL METHODS FAILED for {symbol}: {last_err}")
    if "403" in str(last_err) or "Forbidden" in str(last_err):
        if "failed_sells" not in shared_state:
            shared_state["failed_sells"] = {}
        fails = shared_state["failed_sells"].get(symbol, 0) + 1
        shared_state["failed_sells"][symbol] = fails
        log(f"⚠️ {symbol} sell forbidden after 4 methods (attempt {fails})")
        if fails >= 3:
            if "restricted_positions" not in shared_state:
                shared_state["restricted_positions"] = set()
            shared_state["restricted_positions"].add(symbol)
            log(f"🔒 {symbol} marked RESTRICTED after {fails} failed attempts")
            log(f"   Manual close needed: Alpaca dashboard → Positions → Close {symbol}")
            shared_state["failed_sells"].pop(symbol, None)
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
                record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                             float(pos.get("market_value", 0)), owner.lower(),
                             reason="stop loss triggered", pnl_usd=pnl_usd,
                             pnl_pct=pnl_pct, strategy=strategy,
                             entry_price=entry_price)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
            continue

        # ── STRATEGY A: Dynamic projection take-profit (proj_get_exit_guidance) ─
        if strategy == "A":
            # Try projection_engine dynamic TP first
            should_proj_exit = False
            proj_exit_price  = 0.0
            proj_reason      = ""
            try:
                bars_tp = get_bars(symbol, days=10)
                ind_tp  = compute_indicators(bars_tp) if bars_tp else None
                if ind_tp:
                    guidance = proj_get_exit_guidance(
                        symbol, bars_tp, ind_tp,
                        entry_price, current_price, pnl_pct
                    )
                    if guidance.get("conf", 0) >= 55:
                        should_proj_exit = guidance["should_exit"]
                        proj_exit_price  = guidance["exit_price"]
                        proj_reason      = guidance["reason"]
                        # Also honour projection stop level
                        if current_price <= guidance["stop_price"] and pnl_pct < 0:
                            log(f"🛑 [A-PROJ] [{owner}] PROJ STOP {symbol} "
                                f"below proj_low stop=${guidance['stop_price']:.2f}")
                            if smart_sell(symbol, f"proj stop {guidance['stop_price']}", pos):
                                record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                                             float(pos.get("market_value", 0)), owner.lower(),
                                             reason=f"strategy A proj stop — {proj_reason}",
                                             pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="A-proj",
                                             entry_price=entry_price)
                                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                                shared_state["position_exits"].pop(symbol, None)
                            continue
            except Exception:
                pass  # Fall through to fixed TP

            if should_proj_exit and proj_exit_price > 0:
                log(f"🎯 [A-PROJ] [{owner}] DYNAMIC TP {symbol} "
                    f"+{pnl_pct*100:.1f}% | target=${proj_exit_price:.2f} | {proj_reason}")
                if smart_sell(symbol, f"strategy A dynamic TP — {proj_reason}", pos):
                    record_trade("take_profit", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason=f"strategy A dynamic TP — {proj_reason}",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="A-proj",
                                 entry_price=entry_price)
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    shared_state["position_exits"].pop(symbol, None)
            elif pnl_pct >= RULES["exit_A_take_profit"]:
                # Fixed fallback: original 7% take-profit
                log(f"🎯 [A] [{owner}] FIXED TP {symbol} +{pnl_pct*100:.1f}% >= {RULES['exit_A_take_profit']*100:.0f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, "strategy A take-profit", pos):
                    record_trade("take_profit", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason="strategy A fixed take-profit", pnl_usd=pnl_usd,
                                 pnl_pct=pnl_pct, strategy="A",
                                 entry_price=entry_price)
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

            peak_price     = shared_state["position_exits"][symbol].get("peak_price", current_price)
            trail_stop     = peak_price * (1 - trail_pct)
            profit_at_peak = (peak_price - entry_price) / entry_price

            # Trailing activates only once position hits trail_activates threshold
            trail_active = profit_at_peak >= RULES["exit_B_trail_activates"]

            # ── Fee-aware floor: trailing stop never drops below entry + fees ──
            min_exit_price = min_profitable_exit(entry_price)
            if trail_active and trail_stop < min_exit_price:
                trail_stop = min_exit_price
                log(f"   [B] {symbol}: trail stop floored to ${min_exit_price:.2f} (entry + fees + 0.5%)")

            if trail_active and current_price <= trail_stop:
                log(f"🎯 [B] [{owner}] TRAILING STOP {symbol} | "
                    f"peak=${peak_price:.2f} trail=${trail_stop:.2f} current=${current_price:.2f} | "
                    f"+{pnl_pct*100:.1f}% | +${pnl_usd:.2f}")
                if smart_sell(symbol, f"strategy B trailing stop (peak ${peak_price:.2f})", pos):
                    record_trade("trail_stop", symbol, pos.get("qty"), current_price,
                                 float(pos.get("market_value", 0)), owner.lower(),
                                 reason=f"strategy B trailing stop peak=${peak_price:.2f}",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                 entry_price=entry_price)
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
                            record_trade("time_stop", symbol, pos.get("qty"), current_price,
                                         float(pos.get("market_value", 0)), owner.lower(),
                                         reason=f"strategy B time stop {days_held} days held",
                                         pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                         entry_price=entry_price)
                            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                            shared_state["position_exits"].pop(symbol, None)
                    else:
                        status = f"trailing active, peak=${peak_price:.2f} stop=${trail_stop:.2f}" if trail_active else f"waiting for +3% to activate trail (currently {pnl_pct*100:+.2f}%)"
                        log(f"   [B] {symbol}: {status} | {days_held}d held")
                except Exception: pass

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
    # Read new compact "flags" string or legacy "signals" list — both work
    def _to_signal_list(t):
        flags = t.get("flags") or t.get("f") or ""
        sigs  = t.get("signals", [])
        return [x.strip() for x in flags.split(",") if x.strip()] + list(sigs)
    c_signals   = _to_signal_list(trade_claude)
    g_signals   = _to_signal_list(trade_grok)
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

    # ── Stock tier ────────────────────────────────────────────
    tier         = get_stock_tier(equity)
    tier_focus   = tier.get("focus") or RULES["universe"]
    tier_risk    = tier["risk_pct"]
    trade_budget = round(equity * tier_risk, 2)

    tier_note = (
        f"\n📊 STOCK TIER: {tier['note']}"
        f"\n   Trade budget: {tier_risk*100:.0f}% = ${trade_budget:.2f} per position"
        f"\n   Focus stocks: {', '.join(tier_focus[:5])}"
        f"\n   Swing targets: Stop={RULES['stop_loss_pct']*100:.0f}% | TP={RULES['take_profit_pct']*100:.0f}% | Trail activates at +{RULES['exit_B_trail_activates']*100:.0f}%"
    )

    # ── Breakout scan across universe ────────────────────────
    breakout_stocks = []
    try:
        for sym in tier_focus[:8]:
            proj = shared_state.get("last_projections", {}).get(sym, {})
            ind  = proj.get("indicators", {}) if proj else {}
            if ind.get("breakout_signal") == "BULLISH_BREAKOUT":
                breakout_stocks.append(f"{sym} 🚀")
    except Exception:
        pass
    breakout_note = (f"\n🚀 BREAKOUT STOCKS NOW: {', '.join(breakout_stocks)}"
                     if breakout_stocks else
                     "\n(No confirmed breakouts this cycle — scan for dip entries)")

    # PDT warning for AI
    day_trades_used = shared_state.get("day_trade_count", 0)
    intraday_buys   = shared_state.get("intraday_buys", {})
    pdt_note = ""
    if equity < 25000:
        pdt_note = f"\n⚠️ PDT RULE: Account < $25k → max 3 day trades per 5 days ({day_trades_used}/3 used today)"
        if intraday_buys:
            pdt_note += f"\n   Stocks bought today (selling = day trade): {list(intraday_buys.keys())}"
            pdt_note += f"\n   AVOID selling these today unless stop-loss triggered"
    short_note = short_note + tier_note + breakout_note + pdt_note

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

    # ── Build crypto context for unified R1 call ─────────────
    # If crypto_trader is enabled, gather crypto data and append
    # to R1 prompt — no extra AI call needed
    crypto_context_str = ""
    if crypto_trader.is_enabled():
        try:
            crypto_projs  = crypto_trader.get_projections_snapshot()
            crypto_wallet = crypto_trader.get_wallet_snapshot()
            crypto_stats  = crypto_trader.get_stats_snapshot()
            crypto_cross  = crypto_trader.get_stock_cross_ref(
                shared_state.get("last_projections", {})
            )
            crypto_context_str = prompt_builder.build_crypto_context(
                wallet_summary  = crypto_wallet.get("summary", ""),
                crypto_pool     = crypto_wallet.get("usdt_free", 0),
                crypto_proj_text = crypto_projs,
                crypto_holdings = crypto_wallet.get("holdings_text", ""),
                crypto_stats    = crypto_stats,
                stock_cross_ref = crypto_cross,
            )
            if crypto_context_str:
                log("🪙 Crypto context added to R1 prompt — unified call")
        except Exception as ce:
            log(f"⚠️ Crypto context build failed: {ce} — skipping crypto in R1")

    # Round 1: Both propose independently
    # ── Adaptive prompt — situation-aware, projection-informed, memory-injected ──
    r1_prompt, situation_mode = prompt_builder.build_r1(
        equity          = equity,
        cash            = cash,
        positions       = positions,
        pos_details     = pos_details,
        pool            = pool,
        chart_section   = chart_section,
        news            = news,
        market_ctx      = market_ctx,
        pol_text        = pol_text,
        pol_mimick      = pol_mimick,
        gainers         = gainers,
        ipos            = ipos,
        hot_ipos        = hot_ipos,
        triple_syms     = triple_syms,
        top_collab      = top_collab,
        inv_text        = inv_text,
        short_note      = short_note,
        spy_trend       = shared_state.get("spy_trend", "neutral"),
        features        = features,
        projections     = shared_state.get("last_projections", {}),
        crypto_context  = crypto_context_str,
    )
    log(f"🧠 Prompt mode: {situation_mode.upper().replace('_',' ')}")

    log("🔵 Round 1 — Claude proposing...")
    log("🔴 Round 1 — Grok proposing...")

    c_ok = shared_state["claude_healthy"]
    g_ok = shared_state["grok_healthy"]

    if c_ok:
        claude_r1 = safe_ask_claude(r1_prompt,
            prompt_builder.build_claude_system())
    else:
        log("⚠️ Claude unhealthy — skipping Round 1 for Claude")
        claude_r1 = None

    if g_ok:
        grok_r1 = safe_ask_grok(r1_prompt,
            prompt_builder.build_grok_system())
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

    # ── UNIFIED CRYPTO EXECUTION from R1 responses ────────────
    # Extract crypto_trades from both AI responses and execute now.
    # Zero extra AI calls — crypto piggybacks on the stock R1 call.
    if crypto_trader.is_enabled() and crypto_context_str:
        try:
            crypto_pool_now = crypto_trader.get_wallet_snapshot().get("usdt_free", 0)
            crypto_new = crypto_trader.execute_from_r1(
                claude_r1       = claude_r1,
                grok_r1         = grok_r1,
                crypto_pool     = crypto_pool_now,
                record_trade_fn = record_trade,
                prompt_builder  = prompt_builder,
            )
            if crypto_new:
                log(f"🪙 Crypto: {crypto_new} new position(s) from unified R1")
        except Exception as cex:
            log(f"⚠️ Crypto R1 execution failed: {cex}")

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
JSON: {{"refined_trades":[{{"action":"buy|sell","symbol":"NVDA","notional_usd":15.0,"confidence":85,"f":"flags","r":"<8w>","owner":"claude"}}]}}"""

    g_review_prompt = f"""Your autonomous trades: {g_trades}. Claude's trades: {c_trades}.
Your budget: ${pool['grok']:.2f}. Confirm your best 1-2 trades (owner=grok).
Use Twitter sentiment. No overlap with Claude. Min $8. Confidence 80%+.
JSON: {{"refined_trades":[{{"action":"buy|sell","symbol":"NVDA","notional_usd":15.0,"confidence":85,"f":"flags","r":"<8w>","owner":"grok"}}]}}"""

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

        # ── Symbol validation ─────────────────────────────────
        # Block placeholder/example symbols from JSON templates
        # Only allow 1-5 uppercase letters (real stock tickers)
        import re as _re
        if not _re.match(r'^[A-Z]{1,5}$', symbol):
            log(f"⚠️ Skip {symbol} — invalid ticker format")
            continue
        # Block known placeholder symbols used in prompt examples
        if symbol in {"TICK", "TICKER", "SYM", "SYMBOL", "ABC", "XYZ"}:
            log(f"⚠️ Skip {symbol} — placeholder symbol from prompt template")
            continue

        # ── HARD GUARD: Never route crypto pairs through Alpaca ──
        if is_crypto_symbol(symbol):
            log(f"🚫 BLOCKED: {symbol} is a crypto pair — use Binance.US only, not Alpaca")
            continue

        # ── HARD GUARD: Skip Alpaca-restricted positions ──────
        restricted = shared_state.get("restricted_positions", set())
        if symbol in restricted and action in ("sell", "stop_loss", "take_profit"):
            log(f"🔒 {symbol} RESTRICTED — close manually from Alpaca dashboard")
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
                        record_trade("buy", symbol, shares, limit_price, notional,
                                     owner, confidence=conf, reason="limit order",
                                     strategy=trade.get("exit_strategy","A"))
                        record_intraday_buy(symbol)  # PDT tracking
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
                    record_trade("buy", symbol, None, None, notional,
                                 owner, confidence=conf, reason="market order",
                                 strategy=trade.get("exit_strategy","A"))

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
                # ── Use smart_sell (4-method) instead of raw DELETE ──
                # This handles 403 restrictions, fractional shares, etc.
                pos_data = next((p for p in positions if p["symbol"] == symbol), {})
                if smart_sell(symbol, f"AI sell signal (conf={conf}%)", pos_data):
                    record_trade("sell", symbol, None, None, None,
                                 owner, confidence=conf, reason="AI decision")
                    shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                    shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                    pos_symbols.remove(symbol)
                else:
                    log(f"⚠️ smart_sell failed for {symbol} — all 4 methods tried")
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
    """Check AI health; auto-recover network errors after 30min. Returns (c_ok, g_ok, mode)."""
    try:
        now = datetime.now()
        for ai in ["claude", "grok"]:
            last_fail   = shared_state.get(f"last_{ai}_fail")
            fail_reason = shared_state.get(f"{ai}_fail_reason")
            if last_fail and not shared_state[f"{ai}_healthy"]:
                fail_time = datetime.fromisoformat(last_fail)
                mins_down = (now - fail_time).seconds // 60
                if fail_reason == "credits_exhausted":
                    log(f"💳 {ai.title()} credits exhausted ({mins_down}m) — top-up needed")
                    continue
                elif fail_reason == "auth_error":
                    log(f"🔑 {ai.title()} auth error ({mins_down}m) — check Railway env key")
                    continue
                elif (now - fail_time).seconds >= 1800:
                    shared_state[f"{ai}_healthy"]     = True
                    shared_state[f"{ai}_fail_count"]  = 0
                    shared_state[f"{ai}_fail_reason"] = None
                    log(f"🔄 {ai.title()} auto-recovered after {mins_down}m")
        c_ok     = shared_state["claude_healthy"]
        g_ok     = shared_state["grok_healthy"]
        c_reason = shared_state.get("claude_fail_reason", "")
        g_reason = shared_state.get("grok_fail_reason",  "")
        if c_ok and g_ok:
            mode = None
            log("✅ Both AIs healthy — full collaboration")
        elif c_ok and not g_ok:
            mode = "claude_only"
            log(f"⚠️ FAILOVER: Grok down ({g_reason}) — Claude solo")
        elif g_ok and not c_ok:
            mode = "grok_only"
            log(f"⚠️ FAILOVER: Claude down ({c_reason}) — Grok solo")
        else:
            mode = "autopilot"
            log(f"🆘 BOTH AIs down — AUTOPILOT | Claude:{c_reason} Grok:{g_reason}")
        shared_state["failover_mode"] = mode
        return c_ok, g_ok, mode
    except Exception as e:
        log(f"⚠️ check_ai_health: {e}")
        return True, True, None


def get_cash_thresholds(equity):
    """Return cash thresholds (sleep/watch/active) scaled to equity."""
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
                record_trade("take_profit", symbol, pos.get("qty"), float(pos.get("current_price",0)),
                             float(pos.get("market_value",0)), "bot",
                             reason="autopilot take-profit",
                             pnl_usd=float(pos.get("unrealized_pl",0)),
                             pnl_pct=pnl_pct, strategy="autopilot")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ AUTOPILOT SOLD {symbol}")
            except Exception as e: log(f"❌ {e}")
        elif pnl_pct <= -RULES["stop_loss_pct"]:
            log(f"🛑 AUTOPILOT stop-loss: {symbol} {pnl_pct*100:.1f}%")
            try:
                alpaca("DELETE", f"/v2/positions/{symbol}")
                record_trade("stop_loss", symbol, pos.get("qty"), float(pos.get("current_price",0)),
                             float(pos.get("market_value",0)), "bot",
                             reason="autopilot stop-loss",
                             pnl_usd=float(pos.get("unrealized_pl",0)),
                             pnl_pct=pnl_pct, strategy="autopilot")
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                log(f"✅ AUTOPILOT SOLD {symbol}")
            except Exception as e: log(f"❌ {e}")

    # Only look for buys if we have enough cash
    active_thresh = get_cash_thresholds(equity)["active"]
    if cash < active_thresh:
        log(f"⏳ AUTOPILOT: Cash ${cash:.2f} below active threshold ${active_thresh:.2f} — monitoring only")
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

        # ── projection_engine.py buy scorer ───────────────────
        proj_score, proj_summary = proj_score_buy(sym, bars, ind, cash)

        # ── Intraday signals (VWAP, volume delta, patterns) ───
        intraday_score = 0
        intraday_signals = []
        try:
            intraday = get_intraday_bars(sym, timeframe="5Min", hours=8)
            if intraday and len(intraday) >= 5:
                id_ind = compute_intraday_indicators(intraday)
                if id_ind:
                    # Bonus: price above VWAP = bullish intraday
                    if id_ind["vwap_position"] == "ABOVE_VWAP":
                        intraday_score += 8
                        intraday_signals.append("above_VWAP")
                    # Bonus: buyers dominating volume
                    if id_ind["vol_delta_bias"] == "BUYERS":
                        intraday_score += 8
                        intraday_signals.append(f"buy_vol={id_ind['buy_vol_pct']}%")
                    # Bonus: intraday OBV rising
                    if id_ind["obv_trend"] == "RISING":
                        intraday_score += 5
                        intraday_signals.append("OBV_rising")
                    # Bonus: bullish candlestick pattern
                    bullish_patterns = [p for p in id_ind["patterns"]
                                        if any(x in p for x in
                                        ["HAMMER","ENGULFING","MORNING","SOLDIERS",
                                         "LIQUIDITY_GRAB"])]
                    if bullish_patterns:
                        intraday_score += 10
                        intraday_signals.append(bullish_patterns[0].split("(")[0])
                    # Penalty: price below VWAP + sellers dominating
                    if (id_ind["vwap_position"] == "BELOW_VWAP" and
                            id_ind["vol_delta_bias"] == "SELLERS"):
                        intraday_score -= 15
                        intraday_signals.append("below_VWAP+sellers")
                    # Penalty: bearish patterns
                    bearish_patterns = [p for p in id_ind["patterns"]
                                        if any(x in p for x in
                                        ["SHOOTING_STAR","BEARISH","CROWS",
                                         "EVENING","STOP_HUNT"])]
                    if bearish_patterns:
                        intraday_score -= 10
                        intraday_signals.append(f"BEARISH_PATTERN")
        except Exception:
            pass  # Never break autopilot for intraday failure

        # ── Legacy signals ────────────────────────────────────
        legacy_score = 0
        if ind["rsi"] and ind["rsi"] < RULES["autopilot_rsi_buy"]:
            legacy_score += 10
        if ind["macd"] and ind["macd"] > 0:
            legacy_score += 5
        # OBV divergence from daily
        if ind.get("obv_divergence") == "BULLISH":
            legacy_score += 8
        elif ind.get("obv_divergence") == "BEARISH":
            legacy_score -= 10

        score   = min(100, proj_score + legacy_score + intraday_score)
        signals = [proj_summary] + intraday_signals

        if score >= 55 and score > best_score:
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
                record_trade("buy", sym, None, None, notional, "bot",
                             reason=f"autopilot score={best_score}",
                             strategy="autopilot")
                shared_state["claude_positions"].append(sym)  # Assign to Claude by default
            except Exception as e: log(f"❌ Autopilot buy {sym}: {e}")
    else:
        log(f"🤖 AUTOPILOT: No clear buy signals found — holding cash safely")

def run_low_cash_cycle(positions, pos_symbols, cash, equity, features):
    """Run cycle when cash is low - manage exits only, no new buys."""
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
                record_trade("take_profit", p["symbol"], None, p["price"], p["value"],
                             p["owner"].lower(), reason="low-cash auto take-profit",
                             pnl_usd=p["pnl_usd"], pnl_pct=p["pnl_pct"]/100)
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
                record_trade("stop_loss", p["symbol"], None, p["price"], p["value"],
                             p["owner"].lower(), reason="low-cash auto stop-loss",
                             pnl_usd=p["pnl_usd"], pnl_pct=p["pnl_pct"]/100)
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
            sold_pos = next((p for p in pos_details if p["symbol"] == c_sell), {})
            record_trade("sell", c_sell, None, sold_pos.get("price"), sold_pos.get("value"),
                         sold_pos.get("owner","shared").lower(),
                         reason=f"low-cash: both AIs agreed — {(claude_decision or {}).get('sell_reason','')}",
                         pnl_usd=sold_pos.get("pnl_usd"), pnl_pct=(sold_pos.get("pnl_pct",0)/100 if sold_pos.get("pnl_pct") else None))
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

    # Update month/year rollover and display gains summary
    update_gain_metrics(equity)
    log(format_gains(equity))

    # ── Execute any pending PDT hold councils ─────────────────
    # These were queued when PDT blocked a sell — now AIs are awake
    pending_councils = {k: v for k, v in shared_state.items()
                        if k.startswith("pdt_council_pending_")}
    for key, council in list(pending_councils.items()):
        sym = council.get("symbol", "")
        pos = council.get("pos", {})
        log(f"📊 Running PDT hold council for {sym} (queued from earlier)...")
        try:
            plan = run_pdt_hold_council(sym, pos, ask_claude, ask_grok)
            if plan:
                log(f"   ✅ Council complete: hold {plan.get('hold_days')}d "
                    f"exit=${plan.get('exit_target')} stop=${plan.get('stop_price')}")
        except Exception as e:
            log(f"   ⚠️ Council failed: {e}")
        del shared_state[key]

    # ── Reassess holds with new data if price surged ──────────
    needs_reassess = {k: v for k, v in shared_state.items()
                      if k.startswith("pdt_hold_") and v.get("needs_reassess")}
    for key, plan in list(needs_reassess.items()):
        sym = plan.get("symbol", "")
        log(f"📊 PDT SURGE REASSESS: {sym} price moved significantly — re-running council...")
        try:
            positions = {p["symbol"]: p for p in alpaca("GET", "/v2/positions")}
            if sym in positions:
                new_plan = run_pdt_hold_council(sym, positions[sym], ask_claude, ask_grok)
                if new_plan:
                    log(f"   ✅ Updated plan: hold {new_plan.get('hold_days')}d "
                        f"exit=${new_plan.get('exit_target')}")
            else:
                del shared_state[key]  # Position gone
        except Exception as e:
            log(f"   ⚠️ Reassess failed: {e}")
            shared_state[key]["needs_reassess"] = False

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

    # Daily loss limit — compare to today's starting equity, not all-time budget
    # Use equity at market open (stored in shared_state) as the baseline
    # This prevents false triggers from cumulative losses across days
    day_start_equity = shared_state.get("day_start_equity", equity)
    if day_start_equity <= 0:
        day_start_equity = equity
    loss_pct = (day_start_equity - equity) / day_start_equity if day_start_equity > equity else 0
    if loss_pct >= RULES["daily_loss_limit_pct"]:
        log(f"🛑 Daily loss limit {loss_pct*100:.1f}% — STOPPING today. "
            f"(start=${day_start_equity:.2f} now=${equity:.2f})")
        return

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

            ai_sleep(
                f"brief written — bot monitoring {len(positions_after)} positions + watchlist",
                write_thesis   = True,
                stock_positions = positions_after,
                spy_trend      = shared_state.get("spy_trend", "neutral"),
            )
        else:
            log("⏳ No positions, sufficient cash — AIs staying awake for next opportunity")

    shared_state["last_sync"] = datetime.now().isoformat()
    log("── Cycle complete ──\n")

def run_premarket():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    mins_to_open = max(0, 570 - (now_et.hour * 60 + now_et.minute))
    log(f"📊 PRE-MARKET ({mins_to_open}min to open) — Research phase...")

    # Reset PDT intraday tracking for new day
    reset_intraday_buys_if_new_day()

    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])

    # Reset day_start_equity each morning so daily loss limit is accurate
    shared_state["day_start_equity"] = equity
    prompt_builder._day_start_equity = equity
    # Reset self-repair error counters for fresh day
    try:
        _repair_reset()
    except Exception:
        pass
    update_gain_metrics(equity)
    # Reset crypto day baseline
    try:
        w = crypto_trader.staking  # verify trader exists
        shared_state["crypto_day_start"] = shared_state.get(
            "crypto_day_start", 0)   # will be refreshed on next crypto cycle
        shared_state["crypto_last_day"] = None  # force baseline refresh
    except Exception: pass
    log(f"📅 New day — day_start_equity set to ${equity:.2f}")
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

    research_prompt = prompt_builder.build_premarket(
        equity      = equity,
        pool        = pool,
        chart       = chart,
        news        = news,
        market      = market,
        pol_text    = pol_text,
        pol_mimick  = pol_mimick,
        triple_syms = triple_syms,
        top_collab  = top_collab,
        gainers     = gainers,
        ipos        = ipos,
        hot_ipos    = hot_ipos,
        inv_text    = inv_text,
        projections = shared_state.get("last_projections", {}),
    )

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
        # Full gains summary
        update_gain_metrics(equity)
        log(format_gains(equity))

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
        chart = get_chart_section()   # Refreshes projections + caches in shared_state

        # ── PROJECTION ACCURACY TRACKING (uses track_projection_accuracy from projection_engine) ──
        try:
            for p in positions:
                sym      = p["symbol"]
                actual_h = float(p.get("high_of_day", p.get("current_price", 0)))
                actual_l = float(p.get("low_of_day",  p.get("current_price", 0)))
                if actual_h > 0 and actual_l > 0:
                    track_projection_accuracy(sym, actual_h, actual_l)
            if shared_state["proj_total_count"] > 0:
                log(f"📐 Projection accuracy: "
                    f"{shared_state['proj_hit_count']}/{shared_state['proj_total_count']} = "
                    f"{shared_state['proj_accuracy_pct']}%")
        except Exception as pa:
            log(f"⚠️ Projection accuracy tracking: {pa}")

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
        pol_mimick_ah = intel_ah.get("pol_signals", {}).get("top_mimick", [])

        # Claude reviews smart money for tomorrow
        claude_ah_prompt = prompt_builder.build_afterhours_claude(
            pnl         = pnl,
            positions   = positions,
            pol_text    = pol_text_ah,
            inv_text    = inv_text_ah,
            smart_money = smart_ah,
            spy_trend   = shared_state.get("spy_trend", "neutral"),
        )

        # Grok reviews momentum + politician overlap for tomorrow
        grok_ah_prompt = prompt_builder.build_afterhours_grok(
            pnl         = pnl,
            positions   = positions,
            ipos        = ipos_ah,
            gainers     = gainers_ah,
            news        = news,
            spy_trend   = shared_state.get("spy_trend", "neutral"),
            pol_text    = pol_text_ah,
            pol_mimick  = pol_mimick_ah,
        )

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

        # ── 🔒 STAKING REVIEW (once daily at afterhours) ──────
        # Much better here than mid-cycle — no overlap with stock trading.
        # AIs are already awake for afterhours so no extra wake cost.
        if crypto_trader.is_enabled():
            try:
                log("=" * 50)
                log("🔒 STAKING REVIEW — Daily check at afterhours")
                log("=" * 50)
                crypto_trader.staking.run_staking_cycle(
                    projections   = crypto_trader._projections or {},
                    ask_claude_fn = ask_claude,
                    ask_grok_fn   = ask_grok,
                )
            except Exception as se:
                log(f"⚠️ Staking review error: {se}")

    except Exception as e:
        log(f"❌ After-hours error: {e}")




# ══════════════════════════════════════════════════════════════
# HOURLY TREND SCAN + DEPOSIT DETECTION
# Runs every hour while AIs sleep — pure Alpaca data, zero AI cost
# Stores findings for AI to read when they wake up
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# TRADING BRIEF SYSTEM
# AIs write a full brief before sleeping.
# Bot reads the brief and executes precisely.
# ══════════════════════════════════════════════════════════════

def generate_trading_brief(equity, cash, positions, pool,
                            chart_section, news, market_ctx,
                            pol_text, inv_text, gainers, ipos, smart_money):
    """Generate trading brief - AIs write instructions for the bot."""
    log("=" * 55)
    log("📋 GENERATING TRADING BRIEF — AIs writing instructions for bot")
    log("=" * 55)

    # Include trend scan findings in brief context
    scan_results  = shared_state.get("trend_scan_results", [])
    scan_deposits = [a for a in shared_state.get("trend_alerts",[])
                     if a.get("type") == "deposit"]
    scan_high_pri = [f for f in scan_results if f.get("priority") == "HIGH"]
    scan_summary  = ", ".join([f"{f['symbol']}({f['type']})" for f in scan_high_pri[:5]])
    deposit_note  = f"New deposits: +${sum(d.get('amount',0) for d in scan_deposits):.2f}" if scan_deposits else ""

    if scan_summary:
        log(f"   Bot found while sleeping: {scan_summary}")
    if deposit_note:
        log(f"   {deposit_note}")

    chart_section = get_chart_section()  # Refreshes + caches projections in shared_state
    proj_context  = proj_format_for_ai(shared_state.get("last_projections", {}))

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
Indicators: {chart_section[:300]}

5-LAYER PROJECTIONS from projection_engine.py (use proj_high as TP, proj_low as entry):
{proj_context[:400]}

BOT FOUND WHILE YOU SLEPT (hourly trend scan):
High priority findings: {scan_summary if scan_summary else "none"}
{deposit_note}

Write YOUR PART of the trading brief (account rules + position notes).
Include any trend scan findings in your watchlist if relevant.
Be specific — the bot follows this EXACTLY with no AI to ask.

JSON (compact, no trailing commas, under 500 chars):
{{
  "market_bias": "bullish/bearish/neutral",
  "risk_level": "low/medium/high",
  "max_new_trades": 1,
  "spy_rule": "trade_all",
  "claude_watchlist": [
    {{"symbol":"NVDA","why":"brief","entry_max":0,"strategy":"A/B","confidence":85}}
  ],
  "account_notes": "brief instruction for bot"
}}
  "wake_instructions": [
    {{"type":"price_below","symbol":"NVDA","threshold":170.0,"reason":"approaching support — reassess","priority":"high"}},
    {{"type":"pnl_above","symbol":"PLTR","threshold":5.5,"reason":"near TP — may want to take profit early","priority":"normal"}},
    {{"type":"time_after","threshold":"14:30","reason":"reassess before power hour","priority":"normal"}}
  ]
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

5-LAYER PROJECTIONS (set entry_max = proj_low, TP = proj_high):
{proj_context[:350]}

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
    {{"symbol":"NVDA","why":"IPO momentum/gainer","entry_max":0,"strategy":"B","confidence":85}}
  ],
  "collab_targets": [
    {{"symbol":"NVDA","condition":"both 95%+ on next wake","why":"triple confirmation"}}
  ],
  "sentiment_notes": "key sentiment insight for bot context"
}}"""

    claude_brief = None
    grok_brief   = None

    try:
        claude_brief = safe_ask_claude(claude_brief_prompt,
            "You are Claude writing a trading brief. Respond ONLY with a compact valid JSON object. "
            "No markdown fences, no extra text, no trailing commas. Keep under 500 chars.",
            retries=3)
        if claude_brief:
            log(f"🔵 Claude brief: bias={claude_brief.get('market_bias')} "
                f"risk={claude_brief.get('risk_level')} "
                f"watchlist={[w.get('symbol') for w in claude_brief.get('claude_watchlist',[])]}")
    except Exception as e:
        log(f"❌ Claude brief: {e}")

    try:
        grok_brief = safe_ask_grok(grok_brief_prompt,
            "You are Grok writing a trading brief. Respond ONLY with a compact valid JSON object. "
            "No markdown fences, no extra text, no trailing commas. Keep under 500 chars.",
            retries=3)
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

    # ── Store AI custom wake instructions ─────────────────────
    # Collect from both Claude and Grok briefs
    all_wake_instrs = []
    for brief_src in [claude_brief, grok_brief]:
        if isinstance(brief_src, dict):
            instrs = brief_src.get("wake_instructions", [])
            if isinstance(instrs, list):
                all_wake_instrs.extend(instrs)

    if all_wake_instrs:
        # Deduplicate by type+symbol combo
        seen_keys = set()
        unique_instrs = []
        for instr in all_wake_instrs:
            key = (instr.get("type"), instr.get("symbol",""), instr.get("threshold"))
            if key not in seen_keys:
                seen_keys.add(key)
                unique_instrs.append(instr)
        shared_state["ai_wake_instructions"] = unique_instrs
        log(f"🤖 AI wrote {len(unique_instrs)} custom wake instruction(s):")
        for instr in unique_instrs:
            log(f"   → [{instr.get('priority','normal').upper()}] "
                f"{instr.get('type')} {instr.get('symbol','')} "
                f"@ {instr.get('threshold')} — {instr.get('reason','')[:60]}")
    else:
        shared_state["ai_wake_instructions"] = []

    log(f"📋 TRADING BRIEF COMPLETE:")
    log(f"   Market bias: {shared_state['trading_brief']['account']['market_bias'].upper()}")
    log(f"   Risk level:  {shared_state['trading_brief']['account']['risk_level'].upper()}")
    log(f"   Watchlist:   {[w['symbol'] for w in merged_watchlist]}")
    log(f"   Collab targets: {[t.get('symbol') for t in collab_targets]}")
    log(f"   Position rules: {list(merged_pos_notes.keys())}")
    log(f"   Wake instructions: {len(shared_state['ai_wake_instructions'])}")
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

        # Never buy crypto pairs through Alpaca — Binance.US only
        if is_crypto_symbol(sym):
            log(f"🚫 WATCHLIST BLOCKED: {sym} is crypto — Binance.US only")
            continue
        try:
            bars = get_bars(sym, days=10)
            if not bars:
                continue
            ind           = compute_indicators(bars)
            current_price = bars[-1]["c"]

            # Standard entry_max check
            if entry_max > 0 and current_price > entry_max:
                log(f"⏭️ WATCHLIST: {sym} at ${current_price:.2f} > entry max ${entry_max:.2f} — waiting")
                continue

            # Projection-based entry gate — uses cached projection from shared_state
            # (zero extra API calls needed since get_chart_section already ran)
            proj       = shared_state.get("last_projections", {}).get(sym)
            size_pct_s = size_pct  # default size

            if proj and not proj.get("error") and proj.get("proj_low") and proj.get("confidence", 0) >= 55:
                proj_low  = proj["proj_low"]
                proj_high = proj["proj_high"]
                proj_conf = proj["confidence"]
                proj_bias = proj.get("bias", "neutral")
                range_mid = (proj_low + proj_high) / 2

                # Skip if price already past midpoint on a bearish day
                if current_price > range_mid and proj_bias == "bearish":
                    log(f"⏭️ WATCHLIST PROJ: {sym} ${current_price:.2f} > midrange "
                        f"${range_mid:.2f} bearish — waiting for pullback")
                    continue

                # Reduce size if projection confidence is moderate
                if proj_conf < 60:
                    size_pct_s = size_pct * 0.60
                    log(f"   ⚠️ WATCHLIST: {sym} proj conf={proj_conf} — sizing at 60%")
                else:
                    log(f"   📐 WATCHLIST PROJ: {sym} conf={proj_conf} range={proj_low}–{proj_high} "
                        f"bias={proj_bias} — entry OK")

        except Exception:
            size_pct_s = size_pct
            try:
                current_price = get_bars(sym, days=3)[-1]["c"]
            except Exception:
                continue

        notional = min(remaining_cash * size_pct_s, remaining_cash - 5)
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
            except Exception: pass

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

def ai_sleep(reason="trades executed — waiting for cash threshold",
             write_thesis=False,
             stock_positions=None,
             spy_trend="neutral",
             spy_change_pct=0.0,
             grok_intel=""):
    """
    Put both AIs to sleep. Bot takes over autonomous execution.
    v3.0: optionally writes AI sleep brief (thesis + wake conditions) before sleeping.
    """
    shared_state["ai_sleeping"]     = True
    shared_state["sleep_reason"]    = reason
    shared_state["last_sleep_time"] = datetime.now().isoformat()
    shared_state["wake_reason"]     = None
    shared_state["stops_fired_today"] = 0

    # Store current exit strategies so bot can execute them while AIs sleep
    shared_state["sleeping_strategies"] = dict(shared_state["position_exits"])

    # Clear old scan results so next wake gets fresh data
    shared_state["trend_scan_results"] = []
    shared_state["trend_alerts"]       = []
    shared_state["deposit_detected"]   = False
    shared_state["stops_fired_today"]  = 0

    # ── v3.0: Write AI sleep brief (thesis + custom wake conditions) ──
    if write_thesis and stock_positions is not None:
        try:
            account_now = alpaca("GET", "/v2/account")
            equity_now  = float(account_now["equity"])
            cash_now    = float(account_now["cash"])

            crypto_pos     = getattr(crypto_trader, "positions", {}) or {}
            crypto_wallet  = 0.0
            usdt_free      = 0.0
            wallet_holdings = []
            try:
                from binance_crypto import get_full_wallet
                w = get_full_wallet()
                crypto_wallet   = w.get("total_value", 0.0)
                usdt_free       = w.get("usdt_free", 0.0)
                all_h = w.get("tradeable", []) + w.get("non_tradeable", [])
                wallet_holdings = [
                    {"symbol": h.get("symbol",""), "value": h.get("value_usdt",0),
                     "price": h.get("price",0), "qty": h.get("free",0)}
                    for h in all_h if h.get("value_usdt",0) >= 0.50
                ]
            except Exception:
                pass

            sleep_prompt = build_sleep_brief_prompt(
                stock_positions  = stock_positions or [],
                stock_equity     = equity_now,
                stock_cash       = cash_now,
                crypto_positions = crypto_pos,
                crypto_wallet    = crypto_wallet,
                usdt_free        = usdt_free,
                wallet_holdings  = wallet_holdings,
                spy_trend        = spy_trend,
                spy_change_pct   = spy_change_pct,
                grok_intel       = grok_intel,
                day_pnl_stock    = equity_now - shared_state.get("day_start_equity", equity_now),
                day_pnl_crypto   = shared_state.get("crypto_day_pnl", 0.0),
            )

            log("📋 Writing sleep brief (AI thesis + wake conditions)...")
            raw_brief = ask_claude(
                sleep_prompt,
                system="You are a portfolio manager. Output ONLY valid JSON sleep brief. No prose, no markdown."
            )
            brief_dict = parse_sleep_brief(raw_brief or "")
            if brief_dict:
                thesis_mgr.update_from_sleep_brief(brief_dict, stock_positions, crypto_pos)
                shared_state["portfolio_brief_json"] = brief_dict
                log("✅ Sleep brief written — thesis conditions active")
                for sym, data in brief_dict.get("stocks", {}).items():
                    emerg = data.get("emergency_below")
                    bull  = data.get("bullish_above")
                    log(f"   📍 {sym}: emergency<${emerg:.2f} | bullish>${bull:.2f if bull else 0:.2f}")
                for sym, data in brief_dict.get("crypto", {}).items():
                    emerg = data.get("emergency_below")
                    if emerg:
                        log(f"   📍 {sym}: emergency<${emerg:.6f}")
            else:
                log("⚠️ Sleep brief parse failed — using standard wake conditions")
        except Exception as sb_err:
            log(f"⚠️ Sleep brief error (non-fatal): {sb_err}")

    log(f"😴 AIs going to SLEEP — {reason}")
    log(f"   Bot running autonomously with stored strategies")
    log(f"   Positions covered: {list(shared_state['sleeping_strategies'].keys()) or 'none'}")
    instrs = shared_state.get("ai_wake_instructions", [])
    thesis_count = len(thesis_mgr.get_all_theses())
    if instrs:
        log(f"   🤖 Custom wake triggers ({len(instrs)}):")
        for i in instrs:
            log(f"      • {i.get('type')} {i.get('symbol','')} @ {i.get('threshold')} — {i.get('reason','')[:50]}")
    log(f"   Wake conditions:")
    log(f"     1. Cash crosses active threshold")
    log(f"     2. All positions closed + cash available")
    log(f"     3. 2+ stop-losses fire (market emergency)")
    log(f"     4. 8:30am premarket (always)")
    log(f"     5. SPY drops >2% suddenly (market crash guard)")
    log(f"     6. AI custom instructions ({len(instrs)} active)")
    if thesis_count:
        log(f"     7. Thesis conditions ({thesis_count} positions) [v3.0]")

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
                log(f"   Bot executed {shared_state['stops_fired_today']} stop/TP autonomously")

                # Show what bot found during sleep
                scan_results = shared_state.get("trend_scan_results", [])
                alerts       = shared_state.get("trend_alerts", [])
                deposits     = [a for a in alerts if a.get("type") == "deposit"]
                high_pri     = [f for f in scan_results if f.get("priority") == "HIGH"]

                if scan_results:
                    log(f"   Trend scan found {len(scan_results)} items "
                        f"({len(high_pri)} high priority)")
                    for f in high_pri[:3]:
                        log(f"   ⭐ [{f['type']}] {f['symbol']}: {f['note'][:60]}")
                if deposits:
                    total_dep = sum(d.get("amount",0) for d in deposits)
                    log(f"   💵 New deposits while sleeping: +${total_dep:.2f}")

                # ── v3.0: Show thesis wake context if triggered ───
                thesis_ctx = shared_state.get("thesis_wake_context")
                if thesis_ctx:
                    log(f"   📋 Thesis wake context available for AI review")
            except Exception:
                log(f"🌅 AIs WAKING UP — {reason}")
        else:
            log(f"🌅 AIs WAKING UP — {reason}")

    # ── v3.0: Reset thesis triggered session for fresh checks ──
    try:
        thesis_mgr.reset_triggered_session()
    except Exception:
        pass

def check_wake_conditions(cash, equity, positions, spy_change=0):
    """Check all wake conditions; returns (should_wake, reason). No AI calls."""
    try:
        if not shared_state["ai_sleeping"]:
            return False, None
        thresholds = get_cash_thresholds(equity)

        # ── Cash threshold: only wake on a TRUE CROSSING ──────
        # Cash must have been BELOW threshold before, then gone ABOVE.
        # Prevents waking every 5 min when cash is always above threshold.
        prev_cash = shared_state.get("last_cash", 0.0)
        active    = thresholds["active"]
        if cash >= active and prev_cash < active:
            return True, f"cash ${cash:.2f} crossed active threshold ${active:.2f}"
        if cash >= active and prev_cash == 0.0:
            # First boot — don't wake on cash alone, let 8:30am or other conditions do it
            pass
        if len(positions) == 0 and cash >= thresholds["sleep"]:
            return True, f"all positions closed — ${cash:.2f} available"
        stops = shared_state["stops_fired_today"]
        if stops >= 2:
            return True, f"EMERGENCY — {stops} stop-losses fired"
        if spy_change <= -2.0:
            return True, f"EMERGENCY — SPY dropped {spy_change:.1f}%"
        instr_wake, instr_reason = check_ai_wake_instructions(positions, cash, equity)
        if instr_wake:
            return True, instr_reason

        # ── v3.0: Thesis condition check ─────────────────────
        try:
            crypto_pos = getattr(crypto_trader, "positions", {}) or {}

            # BTC price for correlation tracking
            btc_now = 0.0
            btc_1h  = shared_state.get("btc_price_1h_ago", 0.0)
            try:
                from binance_crypto import get_crypto_price
                btc_now = get_crypto_price("BTCUSDT")
                if not btc_1h:
                    shared_state["btc_price_1h_ago"] = btc_now
                elif (datetime.now() - datetime.fromisoformat(
                        shared_state.get("btc_1h_timestamp",
                        datetime.now().isoformat()))).seconds >= 3600:
                    shared_state["btc_price_1h_ago"]  = btc_now
                    shared_state["btc_1h_timestamp"]  = datetime.now().isoformat()
            except Exception:
                pass

            triggered = thesis_mgr.check_all_conditions(
                stock_positions  = positions,
                crypto_positions = crypto_pos,
                spy_change_pct   = spy_change,
                btc_price_now    = btc_now,
                btc_price_1h_ago = btc_1h,
            )
            if triggered:
                wake_reason_t, wake_ctx = triggered
                shared_state["thesis_wake_reason"]  = wake_reason_t
                shared_state["thesis_wake_context"] = wake_ctx
                log(f"   📋 v3.0 thesis trigger: {wake_reason_t[:80]}")
                return True, wake_reason_t
        except Exception as te:
            log(f"⚠️ Thesis condition check: {te}")

        return False, None
    except Exception as e:
        log(f"⚠️ check_wake_conditions: {e}")
        return False, "check_failed"

def check_ai_wake_instructions(positions, cash, equity):
    """Check AI custom wake instructions against current market state."""
    instructions = shared_state.get("ai_wake_instructions", [])
    if not instructions:
        return False, None

    now_et = datetime.now(ZoneInfo("America/New_York"))
    pos_map = {p["symbol"]: p for p in positions}

    for instr in instructions:
        itype     = instr.get("type", "")
        symbol    = instr.get("symbol", "")
        threshold = instr.get("threshold", 0)
        reason    = instr.get("reason", "AI wake instruction triggered")
        priority  = instr.get("priority", "normal")

        try:
            # ── Price conditions ──────────────────────────────
            if itype == "price_above" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["current_price"]) >= threshold:
                    return True, f"🤖 AI instruction: {symbol} above ${threshold} — {reason}"

            elif itype == "price_below" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["current_price"]) <= threshold:
                    return True, f"🤖 AI instruction: {symbol} below ${threshold} — {reason}"

            # ── P&L conditions ────────────────────────────────
            elif itype == "pnl_above" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["unrealized_plpc"]) * 100 >= threshold:
                    return True, f"🤖 AI instruction: {symbol} P&L +{threshold}% reached — {reason}"

            elif itype == "pnl_below" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["unrealized_plpc"]) * 100 <= threshold:
                    return True, f"🤖 AI instruction: {symbol} P&L {threshold}% hit — {reason}"

            # ── Cash condition ────────────────────────────────
            elif itype == "cash_above":
                if cash >= threshold:
                    return True, f"🤖 AI instruction: cash ${cash:.2f} > ${threshold} — {reason}"

            # ── Time condition ────────────────────────────────
            elif itype == "time_after":
                # threshold is "HH:MM" string
                try:
                    h, m = str(threshold).split(":")
                    target = now_et.replace(hour=int(h), minute=int(m),
                                           second=0, microsecond=0)
                    if now_et >= target:
                        # Remove this instruction after triggering (one-shot)
                        shared_state["ai_wake_instructions"].remove(instr)
                        return True, f"🤖 AI instruction: scheduled wake at {threshold} ET — {reason}"
                except Exception:
                    pass

            # ── SPY conditions ────────────────────────────────
            elif itype in ("spy_above", "spy_below"):
                try:
                    spy_trend, spy_price, _, spy_chg = get_spy_trend()
                    if itype == "spy_above" and spy_price >= threshold:
                        return True, f"🤖 AI instruction: SPY ${spy_price:.2f} above ${threshold} — {reason}"
                    elif itype == "spy_below" and spy_price <= threshold:
                        return True, f"🤖 AI instruction: SPY ${spy_price:.2f} below ${threshold} — {reason}"
                except Exception:
                    pass

        except Exception as ie:
            log(f"⚠️ Wake instruction check error ({itype}): {ie}")

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

    # ── Check PDT hold plans every tick ──────────────────────
    check_pdt_hold_plans()

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

        # ── v3.0: Thesis check — bot needs AI approval to sell ───
        # Exception: if stop-loss is hit AND thesis has no circuit_breaker override,
        # bot still sells at -10% (absolute safety net).
        # For losses between -5% and -10%: thesis controls.
        thesis = thesis_mgr.get_thesis(symbol)
        if thesis and pnl_pct > -RULES["exit_A_stop_loss"]:
            may_sell, sell_reason = thesis_mgr.bot_may_sell(symbol)
            if not may_sell:
                log(f"   📋 {symbol}: AI thesis says HOLD — {sell_reason[:60]}")
                if thesis.emergency_below:
                    log(f"      Emergency level: ${thesis.emergency_below:.2f} "
                        f"({'⚠️ BREACHED' if current_price <= thesis.emergency_below else '✅ safe'})")
                continue   # Skip to next position — bot waits for AI

        # ── UNIVERSAL: Hard stop-loss ─────────────────────────
        if pnl_pct <= -RULES["exit_A_stop_loss"]:
            log(f"🛑 AUTO STOP-LOSS {symbol} {pnl_pct*100:.1f}% — bot executing (AIs sleeping)")
            if smart_sell(symbol, "autonomous stop-loss", pos):
                owner = "claude" if symbol in shared_state["claude_positions"] else "grok"
                record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                             pos_value, owner, reason="autonomous stop-loss (AIs sleeping)",
                             pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy=strategy,
                             entry_price=entry_price)
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
                    owner = "claude" if symbol in shared_state["claude_positions"] else "grok"
                    record_trade("take_profit", symbol, pos.get("qty"), current_price,
                                 pos_value, owner, reason="autonomous strategy A take-profit",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="A",
                                 entry_price=entry_price)
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
                    owner = "claude" if symbol in shared_state["claude_positions"] else "grok"
                    record_trade("trail_stop", symbol, pos.get("qty"), current_price,
                                 pos_value, owner,
                                 reason=f"autonomous strategy B trailing stop peak=${peak_price:.2f}",
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                 entry_price=entry_price)
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
                            owner = "claude" if symbol in shared_state["claude_positions"] else "grok"
                            record_trade("time_stop", symbol, pos.get("qty"), current_price,
                                         pos_value, owner,
                                         reason=f"autonomous time stop {days_held} days held",
                                         pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="B",
                                         entry_price=entry_price)
                            shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                            shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                            shared_state["position_exits"].pop(symbol, None)
                            shared_state["sleeping_strategies"].pop(symbol, None)
                            sold = True
                except Exception: pass

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

    # Initialize day/week/month/year tracking
    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])
    shared_state["day_start_equity"]   = equity
    shared_state["week_start_equity"]  = equity
    shared_state["month_start_equity"] = equity
    shared_state["year_start_equity"]  = equity
    shared_state["last_equity"]        = equity
    update_gain_metrics(equity)

    # Inject shared_state into crypto_trader for gains tracking
    crypto_trader._shared_state = shared_state
    crypto_trader._thesis_mgr   = thesis_mgr    # v3.0: AI-led exits
    crypto_trader._wallet_intel = wallet_intel  # v3.0: opportunity scanner

    last_premarket  = None
    last_afterhours = None

    # Record boot time — used for crypto 2.5min stagger offset
    shared_state["boot_time"] = datetime.now(timezone.utc)
    log("⏱️  Boot time recorded — crypto starts in 2.5 min (staggered)")

    # ── Background monitors (no AI needed) ──────────────────
    cash_check_interval = 60    # Check cash every 60 seconds
    trend_scan_interval = 3600  # Trend scan every 60 minutes
    last_cash_check     = 0
    last_known_cash     = 0
    last_trend_scan     = 0     # Run first scan 1 hour after start

    # Initialize deposit tracking
    try:
        init_acct = alpaca("GET", "/v2/account")
        shared_state["last_equity"] = float(init_acct["equity"])
        shared_state["last_cash"]   = float(init_acct["cash"])
        log(f"💵 Deposit tracker initialized: equity=${shared_state['last_equity']:.2f} "
            f"cash=${shared_state['last_cash']:.2f}")
    except Exception: pass

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
                    shared_state["last_cash"] = current_cash  # Keep in sync for wake condition check

                except Exception as ce:
                    pass  # Silent — cash monitor never crashes the main loop

            # ── MAIN TRADING LOGIC ───────────────────────────

            if mode == "sleep":
                next_check = (now_et + timedelta(minutes=interval)).strftime("%H:%M ET")
                log(f"😴 Sleeping {interval} min. Next: {next_check}")

            elif mode == "premarket":
                # Run once per calendar day — guard prevents re-running every 5-min tick
                _today = datetime.now(ZoneInfo("America/New_York")).date()
                if last_premarket != _today:
                    last_premarket = _today
                    if shared_state["ai_sleeping"]:
                        ai_wake("8:30am premarket — daily research always runs")
                    run_premarket()
                    try:
                        acct_check   = alpaca("GET", "/v2/account")
                        cash_check   = float(acct_check["cash"])
                        eq_check     = float(acct_check["equity"])
                        thresh_check = get_cash_thresholds(eq_check)
                        if cash_check < thresh_check["active"]:
                            pos_check = alpaca("GET", "/v2/positions")
                            ai_sleep(
                                "premarket research done — both AIs sleeping, bot takes over",
                                write_thesis    = True,
                                stock_positions = pos_check,
                                spy_trend       = shared_state.get("spy_trend", "neutral"),
                            )
                    except Exception: pass

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
                # Run once per calendar day — guard prevents re-running every 5-min tick
                _today = datetime.now(ZoneInfo("America/New_York")).date()
                if last_afterhours != _today:
                    last_afterhours = _today
                    if shared_state["ai_sleeping"]:
                        ai_wake("4pm afterhours — daily review always runs")
                    run_afterhours()
                    try:
                        acct_ah   = alpaca("GET", "/v2/account")
                        cash_ah   = float(acct_ah["cash"])
                        eq_ah     = float(acct_ah["equity"])
                        thresh_ah = get_cash_thresholds(eq_ah)
                        pos_ah    = alpaca("GET", "/v2/positions")
                        if cash_ah < thresh_ah["active"] and pos_ah:
                            ai_sleep(
                                "afterhours done — both AIs sleeping, bot guards overnight",
                                write_thesis    = True,
                                stock_positions = pos_ah,
                                spy_trend       = shared_state.get("spy_trend", "neutral"),
                            )
                        elif not pos_ah and cash_ah < thresh_ah["active"]:
                            ai_sleep(
                                "afterhours done — no positions, both AIs sleeping",
                                write_thesis    = True,
                                stock_positions = [],
                                spy_trend       = shared_state.get("spy_trend", "neutral"),
                            )
                    except Exception: pass

        except Exception as e:
            log(f"❌ Loop error: {e}")
            interval = 5

        # ── 🪙 CRYPTO — 24/7 wall-clock timer, staggered 2.5min after stocks ──
        # Offset: crypto first run starts 2.5 min after bot startup.
        # Every run after that is exactly 60 min from last run.
        # This staggers logs cleanly — stocks run first, crypto 2.5 min later.
        try:
            if crypto_trader.is_enabled():
                spy_now     = shared_state.get("spy_trend", "neutral")
                now_utc     = datetime.now(timezone.utc)
                last_run    = shared_state.get("crypto_last_run")
                # Use 'or' not .get() default — shared_state["boot_time"] is None initially
                boot_time   = shared_state.get("boot_time") or now_utc

                # First run: wait 2.5 min after boot so stocks go first
                # Subsequent runs: every 60 min from last run
                if last_run is None:
                    secs_since_boot = (now_utc - boot_time).total_seconds()
                    due = secs_since_boot >= 150  # 2.5 min = 150 seconds
                else:
                    due = (now_utc - last_run).total_seconds() >= 3600

                if due:
                    shared_state["crypto_last_run"] = now_utc
                    if shared_state["ai_sleeping"]:
                        log("🪙 Crypto: AIs sleeping — running standalone cycle")
                    else:
                        log("🪙 Crypto: running hourly cycle")
                    crypto_trader.run_crypto_cycle(
                        total_equity      = 0,
                        ask_claude_fn     = ask_claude,
                        ask_grok_fn       = ask_grok,
                        spy_trend         = spy_now,
                        prompt_builder    = prompt_builder,
                        record_trade_fn   = record_trade,
                        pol_text          = "",
                        stock_projections = shared_state.get("last_projections", {}),
                    )
                else:
                    # Exit monitor every 5-min tick — stops/TPs always protected
                    exits = crypto_trader.run_exit_monitor()
                    if exits:
                        log(f"🪙 Crypto: {exits} autonomous exit(s)")
                    elif last_run is not None:
                        # Countdown to next AI cycle
                        secs_since = (now_utc - last_run).total_seconds()
                        secs_left  = max(0, 3600 - secs_since)
                        m, s       = int(secs_left // 60), int(secs_left % 60)
                        log(f"🪙 Crypto: exit monitor OK | next AI cycle in {m}m {s}s")
                    else:
                        secs_elapsed = (now_utc - boot_time).total_seconds()
                        secs_left    = max(0, 150 - secs_elapsed)
                        log(f"🪙 Crypto: exit monitor OK | first cycle in {int(secs_left)}s")

        except Exception as ce:
            log(f"⚠️ Crypto loop error: {ce}")

        mode, interval = get_market_mode()

        # ── Crypto keeps the bot alive 24/7 ──────────────────
        if crypto_trader.is_enabled() and interval > 5:
            interval = 5

        # ── Countdown timers ─────────────────────────────────
        now_utc          = datetime.now(timezone.utc)
        next_stock_time  = now_utc + timedelta(minutes=interval)
        next_stock_str   = next_stock_time.astimezone(
                               ZoneInfo("America/New_York")
                           ).strftime("%H:%M:%S ET")

        # Crypto: next AI cycle
        crypto_last      = shared_state.get("crypto_last_run")
        boot_time        = shared_state.get("boot_time") or now_utc
        if crypto_last is None:
            # Still waiting for first run (2.5 min stagger)
            secs_elapsed = (now_utc - boot_time).total_seconds()
            secs_left    = max(0, 150 - secs_elapsed)
            next_crypto_str = f"first run in {int(secs_left)}s"
        else:
            secs_since_crypto = (now_utc - crypto_last).total_seconds()
            secs_to_crypto    = max(0, 3600 - secs_since_crypto)
            mins_to_crypto    = int(secs_to_crypto // 60)
            secs_rem          = int(secs_to_crypto % 60)
            next_crypto_ai    = (now_utc + timedelta(seconds=secs_to_crypto)
                                 ).astimezone(ZoneInfo("America/New_York")
                                 ).strftime("%H:%M:%S ET")
            next_crypto_str   = f"{mins_to_crypto}m {secs_rem}s → AI cycle @ {next_crypto_ai}"

        log(f"⏱  Stock next: {interval}m → {next_stock_str} | "
            f"Crypto next: {next_crypto_str}")

        # ── 15-min clean snapshot ─────────────────────────────
        # Prints a clearly-delimited status block every 15 min so
        # you can grep/scroll straight to "═══ SNAPSHOT" in Railway logs.
        last_snap = shared_state.get("last_snapshot_time")
        if last_snap is None or (now_utc - last_snap).total_seconds() >= 900:
            shared_state["last_snapshot_time"] = now_utc
            now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
            _snap_lines = []
            _snap_lines.append(
                f"═══════════════════ 📊 SNAPSHOT {now_et.strftime('%H:%M ET')} "
                f"═══════════════════"
            )
            # ── Account ──────────────────────────────────────
            try:
                acct    = alpaca("GET", "/v2/account")
                equity  = round(float(acct.get("equity", 0)), 2)
                cash    = round(float(acct.get("cash",   0)), 2)
                day_pnl = round(float(acct.get("equity", 0))
                                - shared_state.get("day_start_equity", equity), 2)
                pnl_pct = round(day_pnl / shared_state.get("day_start_equity", equity) * 100, 2) \
                          if shared_state.get("day_start_equity", 0) > 0 else 0.0
                pnl_icon = "🟢" if day_pnl >= 0 else "🔴"
                _snap_lines.append(
                    f"💰 STOCKS  equity=${equity:.2f}  cash=${cash:.2f}  "
                    f"day P&L: {pnl_icon} ${day_pnl:+.2f} ({pnl_pct:+.2f}%)"
                )
            except Exception as _e:
                _snap_lines.append(f"💰 STOCKS  (equity fetch error: {_e})")

            # ── Stock positions ───────────────────────────────
            try:
                s_pos = alpaca("GET", "/v2/positions")
                if s_pos:
                    _snap_lines.append(f"📈 STOCK POSITIONS ({len(s_pos)}):")
                    for p in s_pos:
                        sym      = p["symbol"]
                        entry    = float(p["avg_entry_price"])
                        current  = float(p["current_price"])
                        pnl_p    = round(float(p["unrealized_plpc"]) * 100, 2)
                        pnl_u    = round(float(p["unrealized_pl"]), 2)
                        value    = round(float(p["market_value"]), 2)
                        owner    = "Claude" if sym in shared_state.get("claude_positions", []) else "Grok"
                        icon     = "🟢" if pnl_p >= 0 else "🔴"
                        # position_exits is the live source; sleeping_strategies is backup
                        _exits  = shared_state.get("position_exits", {})
                        _sleeps = shared_state.get("sleeping_strategies", {})
                        strat_lbl = (_exits.get(sym, {}).get("strategy")
                                     or _sleeps.get(sym, {}).get("strategy")
                                     or "A")
                        _snap_lines.append(
                            f"   {icon} [{owner}] {sym}  entry=${entry:.2f} → now=${current:.2f}  "
                            f"P&L={pnl_p:+.2f}% (${pnl_u:+.2f})  val=${value:.2f}  strat={strat_lbl}"
                        )
                else:
                    _snap_lines.append("📈 STOCK POSITIONS: none")
            except Exception as _e:
                _snap_lines.append(f"📈 STOCK POSITIONS (fetch error: {_e})")

            # ── Crypto wallet + positions ─────────────────────
            try:
                if crypto_trader.is_enabled():
                    from binance_crypto import get_crypto_price as _gcp, get_open_crypto_orders as _goo
                    snap   = crypto_trader.get_wallet_snapshot()
                    usdt   = round(snap.get("usdt_free", 0), 2)
                    c_wall = round(snap.get("total_usdt_value", 0), 2)
                    c_day  = shared_state.get("crypto_day_pnl", 0.0)
                    c_icon = "🟢" if c_day >= 0 else "🔴"
                    _snap_lines.append(
                        f"🪙 CRYPTO  wallet=${c_wall:.2f}  USDT free=${usdt:.2f}  "
                        f"day P&L: {c_icon} ${c_day:+.2f}"
                    )

                    # ── Bot-tracked positions (opened via bot) ────
                    c_pos = crypto_trader.positions
                    if c_pos:
                        _snap_lines.append(f"🪙 BOT POSITIONS ({len(c_pos)}):")
                        for sym, pos in c_pos.items():
                            try:
                                cur_px = _gcp(sym)
                            except Exception:
                                cur_px = pos.peak_price
                            p_pnl  = pos.pnl_pct(cur_px)
                            p_icon = "🟢" if p_pnl >= 0 else "🔴"
                            hrs    = round(pos.hours_held(), 1)
                            _snap_lines.append(
                                f"   {p_icon} {sym}  entry=${pos.entry_price:.6f} "
                                f"→ now=${cur_px:.6f}  P&L={p_pnl:+.2f}%  "
                                f"stop=${pos.stop_price:.6f}  TP=${pos.tp_price:.6f}  "
                                f"held={hrs}h  qty={pos.qty}"
                            )

                    # ── All wallet holdings (including external/manual) ──
                    tradeable = snap.get("tradeable", [])
                    if tradeable:
                        _snap_lines.append(f"🪙 WALLET HOLDINGS ({len(tradeable)}):")
                        for h in tradeable:
                            sym   = h.get("symbol", "?")
                            asset = h.get("asset", "?")
                            qty   = h.get("qty", 0)
                            px    = h.get("price", 0)
                            val   = h.get("value_usdt", 0)
                            lock  = h.get("locked", 0)
                            lock_note = f" (🔒{lock:.4f} locked)" if lock > 0.0001 else ""
                            # Check if bot is tracking this
                            bot_tracked = "🤖" if sym in c_pos else "👤"
                            _snap_lines.append(
                                f"   {bot_tracked} {asset}: {qty:.4f} = ${val:.2f} "
                                f"@ ${px:.6f}{lock_note}"
                            )

                    # ── Open limit orders ─────────────────────────
                    try:
                        open_orders = _goo()
                        if open_orders:
                            _snap_lines.append(f"🪙 OPEN ORDERS ({len(open_orders)}):")
                            for o in open_orders:
                                side  = o.get("side", "?")
                                osym  = o.get("symbol", "?")
                                opx   = float(o.get("price", 0))
                                oqty  = float(o.get("origQty", 0))
                                ofill = float(o.get("executedQty", 0))
                                icon  = "🟢" if side == "BUY" else "🔴"
                                _snap_lines.append(
                                    f"   {icon} {side} {osym}  {oqty:.4f} @ ${opx:.6f}  "
                                    f"filled={ofill:.4f}"
                                )
                    except Exception:
                        pass

                    if not tradeable and not c_pos:
                        _snap_lines.append("🪙 CRYPTO: no holdings")
                else:
                    _snap_lines.append("🪙 CRYPTO: disabled")
            except Exception as _e:
                _snap_lines.append(f"🪙 CRYPTO (error: {_e})")

            # ── AI status ─────────────────────────────────────
            ai_state = "😴 SLEEPING" if shared_state.get("ai_sleeping") else "✅ AWAKE"
            _snap_lines.append(f"🤖 AIs: {ai_state}")
            if shared_state.get("ai_sleeping"):
                instr = shared_state.get("ai_wake_instructions", [])
                _snap_lines.append(f"   Wake triggers active: {len(instr)}")

            _snap_lines.append("═" * 60)
            for _line in _snap_lines:
                log(_line)

        log(f"Sleeping {interval} min [mode: {mode}]...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    log(f"🌐 Proxy on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
