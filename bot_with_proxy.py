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

# ── Crypto trading — split out of this process ────────────────
# NovaTrade is stocks-only now. Crypto trading (new entries, staking)
# is being moved to its own standalone bot (see binance_crypto.py's
# __main__ entrypoint). This process still runs the exit monitor so
# any crypto positions already open get managed safely, but it will
# not open new crypto positions. Flip to True only if you intend to
# run crypto and stocks from this same process again.
CRYPTO_TRADING_ENABLED = False

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
    # Fund allocation — Claude is sole decision-maker; Grok is advisory-only
    # (see rebalance_allocations() in portfolio_manager.py, which no longer
    # moves these away from 1.0/0.0)
    "claude_allocation":   1.0,
    "grok_allocation":     0.0,
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
    "grok_balance":            None,   # {remaining_balance, spent_balance, total_granted} or None
    "grok_balance_checked_at": None,
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
    # Misc
    "boot_time":           None,
    "last_cash":           0.0,
    "last_equity":         55.0,
    "last_sync":           "",
    "last_snapshot_time":  None,
    "last_liquidation":    "",
    "liquidation_result":  None,
    "next_buy_target":     None,
    "trading_brief":       {},
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
    get_recent_ipos, get_market_mode, get_penny_stock_movers,
    get_under_25_movers,
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

# ── Health-gated wrappers ─────────────────────────────────────
# A number of call sites below need the raw text/JSON response from
# Claude/Grok (not ask_with_retry's health-tracked wrapper), but were
# calling ask_claude/ask_grok directly with no health check at all.
# That meant a known-dead AI (e.g. Claude marked credits_exhausted)
# kept getting hit every cycle — Round 2 review, low-cash decisions,
# PDT hold council, crypto cycles, staking review each threw their own
# "credit balance too low" error on every tick instead of backing off.
# These wrappers raise immediately when unhealthy so the existing
# try/except around each call site logs one line and moves on, same as
# any other failure, without spending an API call.
def ask_claude_guarded(*args, **kwargs):
    if not shared_state.get("claude_healthy", True):
        raise Exception(f"Claude unhealthy ({shared_state.get('claude_fail_reason','unknown')}) — call skipped")
    try:
        return ask_claude(*args, **kwargs)
    except Exception as e:
        # Mirror safe_ask_claude's classification so a known-dead AI actually
        # gets marked unhealthy here too — otherwise this guard only ever
        # reads the flag and never sets it, so a doomed call (e.g. credits
        # exhausted) keeps firing every cycle instead of backing off.
        error_type = classify_ai_error(str(e))
        shared_state["claude_fail_count"] = shared_state.get("claude_fail_count", 0) + 1
        shared_state["claude_fail_reason"] = error_type
        if error_type == "credits_exhausted":
            shared_state["claude_healthy"]    = False
            shared_state["claude_credits_ok"] = False
            shared_state["last_claude_fail"]  = datetime.now().isoformat()
        elif error_type == "auth_error":
            shared_state["claude_healthy"]   = False
            shared_state["last_claude_fail"] = datetime.now().isoformat()
        elif shared_state["claude_fail_count"] >= RULES["failover_max_retries"]:
            shared_state["claude_healthy"]   = False
            shared_state["last_claude_fail"] = datetime.now().isoformat()
        raise

def ask_grok_guarded(*args, **kwargs):
    if not shared_state.get("grok_healthy", True):
        raise Exception(f"Grok unhealthy ({shared_state.get('grok_fail_reason','unknown')}) — call skipped")
    try:
        return ask_grok(*args, **kwargs)
    except Exception as e:
        # Same as ask_claude_guarded above — see that comment.
        error_type = classify_ai_error(str(e))
        shared_state["grok_fail_count"] = shared_state.get("grok_fail_count", 0) + 1
        shared_state["grok_fail_reason"] = error_type
        if error_type == "credits_exhausted":
            shared_state["grok_healthy"]    = False
            shared_state["grok_credits_ok"] = False
            shared_state["last_grok_fail"]  = datetime.now().isoformat()
        elif error_type == "auth_error":
            shared_state["grok_healthy"]   = False
            shared_state["last_grok_fail"] = datetime.now().isoformat()
        elif shared_state["grok_fail_count"] >= RULES["failover_max_retries"]:
            shared_state["grok_healthy"]   = False
            shared_state["last_grok_fail"] = datetime.now().isoformat()
        raise

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
    get_quick_take_profit_pct,
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
    # last_sync is stamped at the end of every completed run_cycle() —
    # the GitHub health-watchdog workflow (.github/workflows/health-
    # watchdog.yml) reads last_cycle_age_sec to decide whether to advance
    # its last-known-good branch. Without this field the watchdog was
    # conservative-forever: it never promoted, so last-known-good stayed
    # pinned to whatever it was seeded to, and a single failed deploy of
    # an unrelated change reverted this repo all the way back to that
    # stale snapshot instead of the actual last-good state.
    last_sync = shared_state.get("last_sync") or ""
    cycle_age = None
    if last_sync:
        try:
            cycle_age = int((datetime.now() - datetime.fromisoformat(last_sync)).total_seconds())
        except Exception:
            cycle_age = None
    return jsonify({
        "status":             "ok",
        "bot":                BOT_NAME,
        "last_cycle_age_sec": cycle_age,
    })

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
    except Exception:
        results["_volume_mounted"] = False
        results["_volume_contents"] = []

    return jsonify(results)

@app.route("/repair_log")
def repair_log_endpoint():
    """Full Claude Code repair history — saved to /data/repair_log.json"""
    try:
        limit = int(request.args.get("limit", 20))
        if _CC_TRIGGER_AVAILABLE and _cc_trigger:
            summary = _cc_trigger.get_repair_log_summary()
            log_data = _cc_trigger._load_repair_log()
            return jsonify({
                "summary": summary,
                "entries": list(reversed(log_data))[:limit],
                "log_file": "/data/repair_log.json",
            })
        return jsonify({"error": "Claude Code trigger not configured"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/repair_status")
def repair_status_endpoint():
    try:
        status = get_repair_status()
        status["debug"] = {
            "GITHUB_TOKEN_set":  bool(os.environ.get("GITHUB_TOKEN", "")),
            "GITHUB_TOKEN_len":  len(os.environ.get("GITHUB_TOKEN", "")),
            "GITHUB_REPO":       os.environ.get("GITHUB_REPO", "") or "NOT SET",
            "ANTHROPIC_KEY_set": bool(os.environ.get("ANTHROPIC_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")),
        }
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dashboard")
@app.route("/dashboard.html")
def dashboard():
    from flask import send_file, Response
    for path in ["/app/dashboard.html", "./dashboard.html", "dashboard.html"]:
        if os.path.exists(path):
            return send_file(path, mimetype="text/html")
    return Response(
        "<html><body style='background:#0a0c0f;color:#e8eaf0;font-family:monospace;padding:40px'>"
        "<h2>Dashboard not found</h2><p>Upload dashboard.html to GitHub and redeploy.</p>"
        "</body></html>", mimetype="text/html", status=200)

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
            "last_claude_fail":   shared_state.get("last_claude_fail"),
            "grok_healthy":       shared_state["grok_healthy"],
            "grok_credits_ok":    shared_state["grok_credits_ok"],
            "grok_fail_reason":   shared_state["grok_fail_reason"],
            "last_grok_fail":     shared_state.get("last_grok_fail"),
            "grok_balance":            shared_state.get("grok_balance"),
            "grok_balance_checked_at": shared_state.get("grok_balance_checked_at"),
            "penny_candidates":        shared_state.get("last_penny_candidates", []),
            "wider_candidates":        shared_state.get("last_wider_candidates", []),
            "opportunity_scan_at":     shared_state.get("last_opportunity_scan_at"),
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

@app.route("/binance_history")
def binance_history_endpoint():
    """Binance trade history — fetched from exchange, saved to volume."""
    try:
        stats  = get_binance_history_stats()
        trades = _load_binance_history()
        limit  = int(request.args.get("limit", 50))
        return jsonify({
            "stats":  stats,
            "trades": list(reversed(trades))[:limit],
            "file":   "/data/binance_trade_history.json",
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

@app.route("/leaderboard")
def leaderboard():
    """
    Stock trading performance summary.
    Claude is the sole decision-maker now — Grok only reviews Claude's
    proposals (support/risk-check), so there's no more head-to-head
    competition to score. This reports overall stock performance plus
    whether Grok's review pass is currently active.
    """
    try:
        closes = [t for t in trade_history
                  if t.get("pnl_usd") is not None
                  and not (t.get("symbol") or "").upper().endswith(("USDT", "USDC", "BUSD"))]
        wins      = sum(1 for t in closes if (t.get("pnl_usd") or 0) > 0)
        total_pnl = sum(t.get("pnl_usd") or 0 for t in closes)
        win_rate  = round(wins / len(closes) * 100, 1) if closes else 0.0

        recent_trades = [{
            "symbol":  t.get("symbol"),
            "action":  t.get("action"),
            "pnl_usd": t.get("pnl_usd"),
            "pnl_pct": t.get("pnl_pct"),
            "time":    t.get("time"),
        } for t in closes[-10:]]

        open_positions = len(shared_state.get("claude_positions", [])) + len(shared_state.get("grok_positions", []))

        return jsonify({
            "decision_model":  "claude_primary_grok_support",
            "grok_active":     bool(GROK_KEY) and shared_state.get("grok_healthy", True),
            "reserve":         _get_reserve_info(),
            "total_pnl":       round(total_pnl, 2),
            "total_closed":    len(closes),
            "wins":            wins,
            "win_rate":        win_rate,
            "open_positions":  open_positions,
            "recent_trades":   recent_trades,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _get_reserve_info():
    """
    Compute current wallet-scaling reserve based on combined wallet value.
    Returns dict with combined_wallet, reserve_pct, reserve_usd, label.
    Pulls live equity numbers — never raises.
    """
    try:
        # Stocks
        try:
            acct = alpaca("GET", "/v2/account") or {}
            stock_eq = float(acct.get("equity", 0) or 0)
        except Exception:
            stock_eq = 0.0
        # Crypto
        try:
            wallet = binance_crypto.get_full_wallet() or {}
            crypto_eq = float(wallet.get("total_value", 0) or 0)
            usdt_free = float(wallet.get("usdt_free", 0) or 0)
        except Exception:
            crypto_eq, usdt_free = 0.0, 0.0
        combined = stock_eq + crypto_eq
        pct = binance_crypto.get_wallet_reserve_pct(combined)
        return {
