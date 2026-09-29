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
            "combined_wallet":  round(combined, 2),
            "stock_equity":     round(stock_eq, 2),
            "crypto_equity":    round(crypto_eq, 2),
            "reserve_pct":      pct,
            "reserve_usd":      round(usdt_free * pct, 2),
            "tradeable_usdt":   round(usdt_free * (1 - pct), 2),
            "free_threshold":   getattr(binance_crypto, "RESERVE_FREE_THRESHOLD", 1000.0),
            "cap_pct":          getattr(binance_crypto, "RESERVE_CAP_PCT", 0.30),
            "label":            binance_crypto.get_wallet_reserve_label(combined),
        }
    except Exception as e:
        return {"error": str(e)}

@app.route("/memory")
def memory_endpoint():
    """
    AI Memory inspection endpoint — surfaces what the AIs have learned.

    Returns:
      total_closed, total_wins, win_rate_overall — aggregate performance
      lessons_count, symbols_tracked              — memory size
      ai_patterns      → per-AI win rates and best setups
      market_regimes   → bull/bear/neutral performance
      top_symbols      → 10 most-traded symbols with stats
      recent_lessons   → 8 most recent lesson entries
      last_save_iso    → when memory was last persisted to /data
      memory_file      → path on Railway volume

    Example: /memory → JSON with full learning state
    """
    try:
        if not hasattr(prompt_builder, "memory"):
            return jsonify({"error": "memory not initialized"}), 500
        return jsonify(prompt_builder.memory.get_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/core_reserve")
def core_reserve_endpoint():
    """
    Core Reserve status — long-term wealth compounder, walled off from AIs.

    Returns the current reserve composition (BTC/SPY/cash split), target
    allocation, P&L vs total contributions, ATH and entry prices for both
    BTC and SPY, recent contingency events (defensive trims, opportunity
    buys, take-profits, rebalances), and activation status.

    The tactical AIs cannot see this data. It's surfaced only on the
    dashboard so the user can monitor what the long-term layer is doing.
    """
    try:
        if not HAVE_CORE_RESERVE or not core_reserve:
            return jsonify({"enabled": False, "reason": "module not loaded"})
        return jsonify(core_reserve.get_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/evolution")
def evolution_endpoint():
    """
    AI Evolution status — current tier, P&L, eligibility for next tier.

    Pass A: surfaces tier 0 status for both AIs, plus rivalry standings
    and hard-banned phrase list (transparency).
    Pass B (future): will also show pending prompt proposals, audit log,
    and apply/revert history.
    """
    try:
        if not HAVE_AI_EVOLUTION or not ai_evolution:
            return jsonify({"enabled": False, "reason": "module not loaded"})
        # Translate prompt_builder memory stats into the shape ai_evolution expects
        c_stats = {"trades": 0, "total_pnl": 0.0}
        g_stats = {"trades": 0, "total_pnl": 0.0}
        try:
            mem_stats = prompt_builder.memory.get_stats()
            ai_p      = mem_stats.get("ai_patterns", {})
            for ai, dest in (("claude", c_stats), ("grok", g_stats)):
                p = ai_p.get(ai, {})
                w = int(p.get("wins", 0) or 0)
                l = int(p.get("losses", 0) or 0)
                dest["trades"]    = w + l
                dest["total_pnl"] = float(p.get("total_pnl_usd", 0) or 0)
        except Exception:
            pass
        result = ai_evolution.get_full_status(c_stats, g_stats)
        result["enabled"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/strategy")
def strategy_overview_endpoint():
    """
    Strategic brain overview — current state, model registry, both strategies.

    Phase A: Returns enabled=False with state info. Strategists are
    dormant (no API calls, no strategy writes). Useful for verifying
    the integration is wired correctly.
    Phase B: Returns full strategist activity, recent activations,
    cost tracking, and links to per-AI strategy files.
    """
    try:
        if not HAVE_STRATEGIC_BRAIN or not strategic_brain:
            return jsonify({"enabled": False, "reason": "module not loaded"})
        return jsonify(strategic_brain.get_full_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/strategy/<ai_name>")
def strategy_ai_endpoint(ai_name):
    """
    Per-AI strategy file — current strategy + performance + history.

    Phase A: Returns the default Tier-0 strategy (auto-created at boot).
    No API calls, no actual writes. Reading is free.

    URL params:
      /strategy/claude — Claude's strategy file
      /strategy/grok   — Grok's strategy file

    Returns 404 for any other ai_name.
    """
    try:
        if ai_name not in ("claude", "grok"):
            return jsonify({"error": f"unknown AI '{ai_name}'"}), 404
        if not HAVE_STRATEGIC_BRAIN or not strategic_brain:
            return jsonify({"enabled": False, "reason": "module not loaded"})
        return jsonify(strategic_brain.load_strategy(ai_name))
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
    try:
        _repair_scan(msg)
    except Exception:
        pass
    # Feed rolling buffer to Claude Code trigger for log snapshots
    try:
        if _CC_TRIGGER_AVAILABLE and _cc_trigger:
            _cc_trigger.buffer_log_line(str(msg))
    except Exception:
        pass

# ── Inject shared context into extracted modules ──────────────
# Market data needs RULES + log + shared_state
_market_data._set_context(RULES, log, shared_state_ref=shared_state)
# GitHub deploy only needs log
_github_deploy._set_context(log)
# AI clients needs log + shared_state
_ai_clients._set_context(log, shared_state_ref=shared_state)
# Intelligence needs ask_grok + parse_json (now from ai_clients)
_intelligence._set_context(RULES, log,
                            ask_grok_fn   = ask_grok_guarded,
                            parse_json_fn = parse_json)
# PDT manager needs log, shared_state, RULES + all trading functions
# NOTE: smart_sell, record_trade, get_cash_thresholds defined later — see late injection below
# NOTE: sleep_manager also injected late (needs get_cash_thresholds)

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
    "self_repair.py",
    "dashboard.html",
    "thesis_manager.py",
    "wallet_intelligence.py",
    "NOVATRADE_MASTER.md",
    "market_data.py",
    "intelligence.py",
    "github_deploy.py",
    "ai_clients.py",
    "sleep_manager.py",
    "pdt_manager.py",
    "portfolio_manager.py",
]

# [github_get_file_sha → moved to github_deploy.py]
# [github_push_file → moved to github_deploy.py]
# [github_push_all → moved to github_deploy.py]
# Known crypto base symbols — used by is_crypto_symbol() to route trades
_CRYPTO_BASES = {
    "BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC",
    "MATIC", "ATOM", "NEAR", "ALGO", "UNI", "SHIB", "PEPE", "FET", "AUDIO",
    "KAVA", "RVN", "USDT", "USDC", "BUSD", "BNB", "BCH", "ETC", "XLM", "TRX",
    "VET", "SAND", "MANA", "AXS", "AAVE", "GRT", "FIL", "EOS", "CHZ", "FLOW",
    "ICP", "APE", "HBAR", "XTZ", "ZEC", "ENJ", "GALA", "DASH", "QTUM", "OMG",
    "CRV", "1INCH", "COMP", "YFI", "MKR", "SNX", "SUSHI", "BAT", "ZIL", "ONT",
}


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
# [get_trading_pool → portfolio_manager.py]
# [check_autonomy_tier → portfolio_manager.py]
# [get_autonomy_status → portfolio_manager.py]
# [rebalance_autonomy_funds → portfolio_manager.py]
# [rebalance_allocations → portfolio_manager.py]
# [_save_all_persistent_state → portfolio_manager.py]
# [update_gain_metrics → portfolio_manager.py]
# [format_gains → portfolio_manager.py]
# [track_pnl → portfolio_manager.py]
# [check_account_features → portfolio_manager.py]
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

# [get_news_context → moved to market_data.py / intelligence.py]
# [get_fear_greed_index → moved to market_data.py / intelligence.py]
# [get_earnings_calendar → moved to market_data.py / intelligence.py]
# [_trim_trade_history_to_6months → portfolio_manager.py]
def estimate_fees(notional):
    return round(max(notional * 0.0000278, 0.01) + min(notional * 0.000145, 7.27), 4)

def min_profitable_exit(entry_price: float, fee_pct: float = 0.0003,
                         min_profit_pct: float = 0.005) -> float:
    """Calculate minimum sell price that covers fees and slippage."""
    return round(entry_price * (1 + fee_pct + min_profit_pct), 2)

# ── AI Calls ─────────────────────────────────────────────
# [ask_claude → moved to ai_clients.py]
# [ask_grok → moved to ai_clients.py]
# [clean_json_str → moved to ai_clients.py]
# [_expand_r1_keys → moved to ai_clients.py]
# [parse_json → moved to ai_clients.py]
# [ask_with_retry → moved to ai_clients.py]
def is_market_open():
    return alpaca("GET", "/v2/clock").get("is_open", False)

# [get_market_mode → moved to market_data.py / intelligence.py]
def get_trail_pct(symbol):
    """Get volatility-adjusted trailing stop percentage for a stock"""
    if symbol in RULES["volatile_stocks"]:
        return RULES["exit_B_trail_volatile"]   # 8% for volatile
    elif symbol in RULES["stable_stocks"]:
        return RULES["exit_B_trail_stable"]     # 3% for stable
    return RULES["exit_B_trail_default"]        # 5% default

def stock_turtle_check_entry(symbol: str, system: int = 1) -> dict:
    """
    Check if a STOCK is a valid Turtle entry RIGHT NOW.
    Fetches daily bars via existing get_bars helper.
    Returns same shape as binance_crypto.turtle_check_entry().
    """
    try:
        from turtle_math import compute_turtle_signal
    except ImportError as e:
        return {"eligible": False, "reason": f"turtle_math import failed: {e}",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    try:
        # get_bars is imported at top of bot_with_proxy.py from market_data.
        # 90 days of daily bars: plenty for 55-day Donchian + ATR(20).
        bars = get_bars(symbol, days=90)
    except Exception as e:
        return {"eligible": False, "reason": f"bar fetch failed: {e}",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    if not bars or len(bars) < 56:
        return {"eligible": False, "reason": f"insufficient history ({len(bars) if bars else 0}/56 bars)",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    sig = compute_turtle_signal(bars, system=system)
    if sig is None:
        return {"eligible": False, "reason": "could not compute signal",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    period = 20 if system == 1 else 55
    if sig["entry_signal"]:
        return {"eligible": True,
                "reason": f"{period}d breakout: ${sig['current_close']:.2f} > ${sig['entry_level']:.2f}",
                "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
                "atr": sig["atr"], "system": system,
                "donchian_high": sig["entry_level"]}
    return {"eligible": False,
            "reason": f"no {period}d breakout: ${sig['current_close']:.2f} ≤ ${sig['entry_level']:.2f}",
            "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
            "atr": sig["atr"], "system": system,
            "donchian_high": sig["entry_level"]}


def stock_turtle_check_exit(symbol: str, entry_price: float, atr_at_entry: float,
                           system: int = 1) -> dict:
    """Check if a Turtle stock position should be closed now."""
    try:
        from turtle_math import should_turtle_exit
    except ImportError as e:
        return {"should_exit": False, "reason": f"turtle_math import failed: {e}",
                "exit_level": None}
    try:
        bars = get_bars(symbol, days=90)
    except Exception as e:
        return {"should_exit": False, "reason": f"bar fetch failed: {e}", "exit_level": None}
    if not bars:
        return {"should_exit": False, "reason": "no bars", "exit_level": None}
    return should_turtle_exit(bars, entry_price, atr_at_entry, system=system)


def is_turtle_active_for_stocks() -> bool:
    """
    Returns True iff EITHER AI's STOCK playbook has strategy_type='turtle'.
    Stocks are shared between Claude and Grok, so we activate Turtle on
    stocks whenever either AI's stock playbook says so. Reads the
    per-asset-class playbook — the crypto playbook (mean-reversion)
    must never influence how stocks pick trades.
    """
    try:
        import strategic_brain as _sb
        for ai_name in ("claude", "grok"):
            try:
                cs = _sb.load_strategy_for(ai_name, "stock") or {}
                if cs.get("strategy_type") == "turtle":
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def assign_exit_strategy(symbol, strategy, entry_price, confidence=80, rationale="",
                        atr_at_entry=None, donchian_high=None, system=1):
    """
    Assign exit strategy to a position when it's opened.
    Strategy A = fixed take-profit (fast trades, news-driven)
    Strategy B = trailing stop (momentum/trend plays, let winners run)
    Strategy T = Turtle (2N ATR stop + Donchian breakdown exit, no TP)
    """
    trail_pct = get_trail_pct(symbol)
    cfg = {
        "strategy":    strategy,
        "entry_price": entry_price,
        "peak_price":  entry_price,   # Tracks highest price seen
        "entry_date":  datetime.now().strftime("%Y-%m-%d"),
        "trail_pct":   trail_pct,
        "confidence":  confidence,
        "rationale":   rationale[:100],
    }
    if strategy == "T":
        cfg["atr_at_entry"]  = atr_at_entry
        cfg["donchian_high"] = donchian_high
        cfg["turtle_system"] = system
        if atr_at_entry:
            stop_price = round(entry_price - (2 * atr_at_entry), 4)
            cfg["stop_price"] = stop_price
            log(f"📋 {symbol} exit strategy: T (Turtle System {system}, 2N stop ${stop_price:.2f}, ATR=${atr_at_entry:.2f}) — {rationale[:60]}")
        else:
            log(f"📋 {symbol} exit strategy: T (Turtle, no ATR recorded) — {rationale[:60]}")
    elif strategy == "A":
        log(f"📋 {symbol} exit strategy: A (fixed {RULES['exit_A_take_profit']*100:.0f}% TP) — {rationale[:60]}")
    else:
        log(f"📋 {symbol} exit strategy: B (trailing {trail_pct*100:.0f}% stop, {RULES['exit_B_time_stop_days']}d time) — {rationale[:60]}")
    shared_state["position_exits"][symbol] = cfg

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

# [get_spy_trend → moved to market_data.py / intelligence.py]
# [record_intraday_buy → pdt_manager.py]
# [is_day_trade → pdt_manager.py]
# [get_stock_tier → pdt_manager.py]
# [reset_intraday_buys_if_new_day → pdt_manager.py]
# [check_pdt_safe → pdt_manager.py]
# [run_pdt_hold_council → pdt_manager.py]
# [_pdt_fallback_plan → pdt_manager.py]
# [check_pdt_hold_plans → pdt_manager.py]
# [get_pdt_decision → pdt_manager.py]
# [get_pdt_status → pdt_manager.py]

# ══════════════════════════════════════════════════════════════
# BROKER-SIDE PROTECTIVE STOPS (P0b)
# Every long stock position gets a real resting STOP order at
# Alpaca so downside protection survives bot restarts/outages.
# A resting stop HOLDS the shares — every sell/close path must
# cancel it first (see cancel_stock_orders calls in exit paths).
# ══════════════════════════════════════════════════════════════

def _is_option_symbol(symbol):
    """OCC option symbols end in C/P + 8-digit strike (e.g. AAPL240119C00190000)."""
    return len(symbol) > 12 and symbol[-9] in ("C", "P") and symbol[-8:].isdigit()

def get_open_stock_orders(symbol=None):
    """List open Alpaca orders, optionally filtered to one symbol."""
    try:
        path = "/v2/orders?status=open&limit=500"
        if symbol:
            path += f"&symbols={symbol}"
        return alpaca("GET", path) or []
    except Exception as e:
        log(f"⚠️ [STOP] open-orders fetch failed ({symbol or 'all'}): {e}")
        return []

def cancel_stock_orders(symbol, why=""):
    """Cancel ALL open orders for a symbol. Returns count cancelled.
    Never raises — safe to call unconditionally before any sell/close."""
    n = 0
    for o in get_open_stock_orders(symbol):
        try:
            alpaca("DELETE", f"/v2/orders/{o['id']}")
            log(f"   [STOP] Cancelled {o.get('type','?')} {o.get('side','?')} "
                f"order for {symbol} (id={str(o.get('id',''))[:8]}...) {why}")
            n += 1
        except Exception as ce:
            log(f"   ⚠️ [STOP] Cancel failed {symbol} {str(o.get('id',''))[:8]}: {ce}")
    return n

def compute_protective_stop_price(symbol, entry_price):
    """Stop level mirroring the software exit logic: Turtle 2N stop if
    assigned in position_exits, else the universal hard stop."""
    cfg = shared_state.get("position_exits", {}).get(symbol, {})
    sp = cfg.get("stop_price")
    if sp and sp > 0:
        return sp
    return entry_price * (1 - RULES["exit_A_stop_loss"])

def place_stock_protective_stop(symbol, qty, stop_price):
    """Submit a resting STOP sell at Alpaca. Tries GTC first, falls back
    to DAY (fractional GTC support varies). Non-fatal on failure — the
    software stop monitor stays active either way. Returns order or None."""
    if _is_option_symbol(symbol):
        log(f"   [STOP] {symbol} looks like an option — software-managed only")
        return None
    if qty != int(qty):
        log(f"   [STOP] {symbol} qty={qty} is fractional — Alpaca rejects stop "
            f"orders on fractional share quantities, software stop still active")
        return None
    # Alpaca price increments: $0.01 at/above $1, $0.0001 below
    stop_price = round(stop_price, 2) if stop_price >= 1 else round(stop_price, 4)
    if stop_price <= 0 or qty <= 0:
        log(f"   [STOP] invalid stop for {symbol} (qty={qty}, stop={stop_price}) — skipped")
        return None
    for tif in ("gtc", "day"):
        try:
            order = alpaca("POST", "/v2/orders", {
                "symbol": symbol, "qty": str(qty),
                "side": "sell", "type": "stop",
                "stop_price": str(stop_price),
                "time_in_force": tif,
            })
            log(f"   [STOP] Broker stop resting for {symbol}: {qty} @ ${stop_price} "
                f"({tif.upper()}, order {str(order.get('id',''))[:8]}...)")
            return order
        except Exception as se:
            log(f"   [STOP] {tif.upper()} stop rejected for {symbol}: {str(se)[:120]}")
    log(f"   [STOP] NOT placed for {symbol} -- software stop still active")
    return None

def _arm_stop_after_buy(order, symbol, fallback_entry=0):
    """After a BUY submits, wait briefly for the fill and place the broker
    stop. Unfilled orders are picked up by ensure_protective_stops later."""
    try:
        time.sleep(2)
        od = alpaca("GET", f"/v2/orders/{order['id']}")
        filled_qty = float(od.get("filled_qty") or 0)
        if od.get("status") == "filled" and filled_qty > 0:
            entry = float(od.get("filled_avg_price") or 0) or fallback_entry
            if entry > 0:
                place_stock_protective_stop(
                    symbol, filled_qty,
                    compute_protective_stop_price(symbol, entry))
                return
        log(f"   [STOP] {symbol} buy not filled yet ({od.get('status','?')}) — "
            f"stop deferred to reconciler")
    except Exception as e:
        log(f"   [STOP] arm-after-buy error {symbol}: {e} — reconciler will cover")

def ensure_protective_stops(positions):
    """Reconciler — runs every cycle. (1) Cancels DANGLING stop orders
    (symbol no longer held) so a triggered stray stop can never sell
    shares we don't have. (2) Places a missing stop for any long stock
    position without one (catches late limit fills, restarts, manual
    cancels). (3) Cleans tracker state for positions that vanished —
    stop filled at the broker while the bot was down."""
    try:
        open_orders = get_open_stock_orders()
        held = {p["symbol"]: p for p in positions}

        # 1. Dangling stop sells → cancel
        for o in open_orders:
            if "stop" in (o.get("type") or "") and o.get("side") == "sell" \
                    and o.get("symbol") not in held:
                try:
                    alpaca("DELETE", f"/v2/orders/{o['id']}")
                    log(f"   [STOP] Cancelled DANGLING stop for {o.get('symbol')} "
                        f"(no position — exit/stop already filled)")
                except Exception as ce:
                    log(f"   ⚠️ [STOP] Dangling cancel failed {o.get('symbol')}: {ce}")

        # 2. Unprotected longs → place stop
        stop_syms = {o.get("symbol") for o in open_orders
                     if "stop" in (o.get("type") or "") and o.get("side") == "sell"}
        sell_syms = {o.get("symbol") for o in open_orders if o.get("side") == "sell"}
        for sym, p in held.items():
            if _is_option_symbol(sym):
                continue
            qty = float(p.get("qty", 0) or 0)
            if qty <= 0:
                continue  # shorts stay software-managed for now
            if sym in stop_syms:
                continue  # already protected
            if sym in sell_syms:
                continue  # an exit sell is already working — never double-sell
            qty_avail = float(p.get("qty_available", qty) or 0)
            if qty_avail <= 0:
                log(f"   [STOP] {sym}: no available qty (held by other orders) — skipped")
                continue
            entry = float(p.get("avg_entry_price", 0) or 0)
            if entry <= 0:
                continue
            place_stock_protective_stop(sym, qty_avail,
                                        compute_protective_stop_price(sym, entry))

        # 3. Vanished positions → clean trackers
        open_syms = {o.get("symbol") for o in open_orders}
        for sym in list(shared_state.get("position_exits", {}).keys()):
            if sym not in held and sym not in open_syms:
                log(f"   [STOP] {sym} tracked but no position/orders — "
                    f"broker stop likely filled; cleaning trackers")
                shared_state["position_exits"].pop(sym, None)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != sym]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != sym]
    except Exception as e:
        log(f"⚠️ ensure_protective_stops error: {e}")

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

    # ── Cancel any resting broker stop first — it HOLDS the shares and
    # would block every sell method below (and could double-sell later).
    cancel_stock_orders(symbol, "(before exit sell)")

    last_err = None
    # Method 1: Limit sell at mid-price
    try:
        snap_url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
        headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        snap_res = requests.get(snap_url, headers=headers, timeout=5)
        if snap_res.ok:
            quote = snap_res.json().get("quote", {})
            bid   = float(quote.get("bp", 0))
            ask   = float(quote.get("ap", 0))
            if bid > 0 and ask > 0:
                sell_price = round((bid + ask) / 2, 2)
                qty = pos.get("qty", pos.get("qty_available", "1"))
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
        log(f"   ⚠️ Method 1 (limit) failed: {last_err[:80]}")
    # Method 2: Market via DELETE
    try:
        alpaca("DELETE", f"/v2/positions/{symbol}")
        log(f"✅ MARKET SELL {symbol} (DELETE) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 2 (DELETE) failed: {last_err[:80]}")
    # Method 3: Market via POST
    try:
        qty = pos.get("qty", pos.get("qty_available", "1"))
        alpaca("POST", "/v2/orders", {
            "symbol": symbol, "qty": str(qty),
            "side": "sell", "type": "market", "time_in_force": "day",
        })
        log(f"✅ MARKET SELL {symbol} (POST) — {reason}")
        shared_state.get("failed_sells", {}).pop(symbol, None)
        return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 3 (POST market) failed: {last_err[:80]}")
    # Method 4: Notional sell
    try:
        market_val = float(pos.get("market_value", 0))
        if market_val > 0:
            alpaca("POST", "/v2/orders", {
                "symbol": symbol,
                "notional": str(round(market_val, 2)),
                "side": "sell", "type": "market", "time_in_force": "day",
            })
            log(f"✅ NOTIONAL SELL {symbol} ${market_val:.2f} — {reason}")
            shared_state.get("failed_sells", {}).pop(symbol, None)
            return True
    except Exception as e:
        last_err = str(e)
        log(f"   ⚠️ Method 4 (notional) failed: {last_err[:80]}")
    # All methods failed — mark restricted after 3 attempts
    log(f"❌ ALL SELL METHODS FAILED for {symbol}: {last_err}")
    if "403" in str(last_err) or "Forbidden" in str(last_err):
        if "failed_sells" not in shared_state:
            shared_state["failed_sells"] = {}

        fails = shared_state["failed_sells"].get(symbol, 0) + 1
        shared_state["failed_sells"][symbol] = fails
        if fails >= 3:
            if "restricted_positions" not in shared_state:
                shared_state["restricted_positions"] = set()
            shared_state["restricted_positions"].add(symbol)
            log(f"🔒 {symbol} marked RESTRICTED after {fails} failed attempts — close manually in Alpaca")
            shared_state["failed_sells"].pop(symbol, None)
    return False

def check_exit_conditions(positions, equity):
    """
    Strategy-aware exit system.
    Each position uses whichever strategy was assigned at entry:
    Strategy A: Fixed 7% take-profit + 4% stop + no time limit
    Strategy B: Trailing stop + 4% hard stop + 3-day time stop
    """
    today = datetime.now().strftime("%Y-%m-%d")
    quick_tp = get_quick_take_profit_pct(equity)

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

        # ── UNIVERSAL: Quick take-profit, all strategies ──────
        # Bank any real gain instead of waiting for the strategy-specific
        # 8% target — maximize round-trips and keep cash working.
        # Applies to Turtle (strategy T) too now — every tier has a
        # tp_pct (see portfolio_manager.py stock_tiers), so this always
        # fires before the A/B/T-specific logic below gets a chance to
        # hold out for a bigger, slower move.
        if quick_tp is not None and pnl_pct >= quick_tp:
            log(f"⚡ [{owner}] QUICK TP {symbol} +{pnl_pct*100:.1f}% >= {quick_tp*100:.1f}% "
                f"(tier: equity ${equity:.0f}) | +${pnl_usd:.2f}")
            if smart_sell(symbol, "quick take-profit (small-account velocity)", pos):
                record_trade("take_profit", symbol, pos.get("qty"), current_price,
                             float(pos.get("market_value", 0)), owner.lower(),
                             reason=f"quick take-profit (tier tp={quick_tp*100:.1f}%)",
                             pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy=strategy,
                             entry_price=entry_price)
                shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                shared_state["position_exits"].pop(symbol, None)
            continue

        # ── STRATEGY T: Turtle (2N ATR stop + Donchian breakdown) ─
        # Turtle bypasses A/B entirely. The ONLY two exit conditions are:
        #   1. Price hits the 2N stop set at entry (a 1-unit loss)
        #   2. Price closes below the 10-day Donchian low (or 20-day for System 2)
        # No fixed TP. No trailing. No time stop.
        if strategy == "T":
            t_atr    = exit_cfg.get("atr_at_entry")
            t_system = exit_cfg.get("turtle_system", 1)
            if t_atr and t_atr > 0:
                try:
                    t_exit = stock_turtle_check_exit(symbol, entry_price, t_atr, system=t_system)
                except Exception as _te:
                    log(f"   ⚠️ [T] {symbol}: exit check failed: {_te}")
                    t_exit = {"should_exit": False, "reason": "exit check error"}

                if t_exit.get("should_exit"):
                    reason = t_exit.get("reason", "turtle exit")
                    log(f"🐢 [T] [{owner}] TURTLE EXIT {symbol} {pnl_pct*100:+.1f}% — {reason}")
                    if smart_sell(symbol, f"turtle exit — {reason}", pos):
                        # Distinguish stop vs trend-end for recording
                        is_stop = "2N stop" in reason
                        record_trade("stop_loss" if is_stop else "take_profit",
                                     symbol, pos.get("qty"), current_price,
                                     float(pos.get("market_value", 0)), owner.lower(),
                                     reason=f"turtle {reason}",
                                     pnl_usd=pnl_usd, pnl_pct=pnl_pct, strategy="T",
                                     entry_price=entry_price)
                        shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                        shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                        shared_state["position_exits"].pop(symbol, None)
                else:
                    # Show current 2N stop level for log clarity
                    stop2n = entry_price - (2 * t_atr)
                    log(f"   🐢 [T] {symbol}: {pnl_pct*100:+.2f}% | 2N stop=${stop2n:.2f} | holding")
            else:
                # ATR missing — degrade to a simple 8% stop, don't get stuck
                if pnl_pct <= -0.08:
                    log(f"🛑 [T-FALLBACK] {symbol} {pnl_pct*100:.1f}% — no ATR, using 8% fallback stop")
                    if smart_sell(symbol, "turtle fallback stop (no ATR)", pos):
                        record_trade("stop_loss", symbol, pos.get("qty"), current_price,
                                     float(pos.get("market_value", 0)), owner.lower(),
                                     reason="turtle fallback stop", pnl_usd=pnl_usd,
                                     pnl_pct=pnl_pct, strategy="T",
                                     entry_price=entry_price)
                        shared_state["claude_positions"] = [s for s in shared_state["claude_positions"] if s != symbol]
                        shared_state["grok_positions"]   = [s for s in shared_state["grok_positions"]   if s != symbol]
                        shared_state["position_exits"].pop(symbol, None)
                else:
                    log(f"   🐢 [T] {symbol}: {pnl_pct*100:+.2f}% (no ATR — fallback monitoring)")
            continue   # Don't fall through to A/B

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

# ── Decision Engine (Claude primary, Grok support/review) ────

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

    # ── Sub-$5 + under-$25 opportunities: scan + Grok research ──
    # Deliberately separate from RULES["universe"] (which stays
    # "no OTC/penny tickers") — these are explicit, higher-risk
    # opportunistic channels Claude sees as extra context, not a
    # change to the core watchlist.
    penny_candidates = []
    wider_candidates = []
    penny_research   = ""
    try:
        penny_candidates = get_penny_stock_movers()
    except Exception as pe:
        log(f"⚠️ Penny stock scan failed: {pe}")
    try:
        wider_candidates = get_under_25_movers()
    except Exception as we:
        log(f"⚠️ Under-$25 scan failed: {we}")

    # Cache latest scans for the dashboard — informational only, not
    # re-read by the trading logic itself.
    shared_state["last_penny_candidates"] = penny_candidates
    shared_state["last_wider_candidates"] = wider_candidates
    shared_state["last_opportunity_scan_at"] = datetime.now().isoformat()

    # One combined Grok research call covers both lists (dedup by
    # symbol) — no reason to spend two AI calls on overlapping names.
    research_candidates = list({c["symbol"]: c for c in (penny_candidates + wider_candidates)}.values())
    if research_candidates and shared_state.get("grok_healthy", True):
        try:
            log(f"🔴 Grok researching {len(research_candidates)} sub-$25 mover(s) on X/news...")
            penny_research = ask_grok_guarded(
                prompt_builder.build_penny_research_prompt(research_candidates),
                prompt_builder.build_penny_research_system(),
            )
            if penny_research:
                log(f"🔴 Opportunity research: {len(penny_research)} chars returned")
        except Exception as pre:
            log(f"⚠️ Opportunity research failed: {pre}")
            penny_research = ""

    # Crypto trading has been split out of the stock decision cycle —
    # see binance_crypto.py's standalone entrypoint. No crypto context
    # is built or piggybacked onto the stock R1 call any more.

    # ── Round 1: Claude proposes (sole decision-maker) ─────────
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
        crypto_context  = "",
        penny_stocks    = penny_candidates,
        penny_research  = penny_research,
        wider_stocks    = wider_candidates,
    )
    log(f"🧠 Prompt mode: {situation_mode.upper().replace('_',' ')}")

    log("🔵 Round 1 — Claude proposing (primary decision-maker)...")

    c_ok = shared_state["claude_healthy"]
    if not c_ok:
        log("⚠️ Claude unhealthy — no trade this cycle (Grok is support-only, never trades solo)")
        return [], False, {}

    claude_r1 = safe_ask_claude(r1_prompt, prompt_builder.build_claude_system())
    if not claude_r1:
        log("⚠️ Claude Round 1 failed — holding")
        return [], False, {}

    log(f"🔵 Claude: '{claude_r1.get('strategy_name','')}' | {len(claude_r1.get('proposed_trades',[]))} trades")

    # ── Round 2 — Grok reviews Claude's proposal (support role only) ──
    # Grok no longer trades its own fund or proposes independent trades;
    # it's a second-opinion / risk-check on Claude's picks.
    c_trades = [(t.get("symbol"),t.get("confidence"),t.get("direction","long"))
                for t in (claude_r1 or {}).get("proposed_trades",[])]

    g_ok = shared_state.get("grok_healthy", True)
    grok_review = None
    if g_ok and c_trades:
        log("🔴 Round 2 — Grok reviewing Claude's proposal (support role)...")
        g_review_prompt = f"""Claude is proposing these trades this cycle: {c_trades}.
You are Grok, acting as a SUPPORT / second-opinion risk-check — you do NOT trade your own fund.
Use X/Twitter sentiment and news to flag risk on each symbol.
JSON only: {{"reviewed":[{{"symbol":"NVDA","verdict":"confirm|caution|veto","note":"<12w>"}}]}}"""
        grok_review = ask_with_retry(ask_grok_guarded, g_review_prompt,
            "You are Grok, a support/risk-check reviewer only — not an independent trader. ONLY valid JSON under 400 chars.")
        if grok_review:
            log(f"🔴 Grok review: {len(grok_review.get('reviewed',[]))} trade(s) reviewed")
    elif not g_ok:
        log("⚠️ Grok unhealthy — proceeding on Claude's proposal alone")

    vetoed = set()
    for r in (grok_review or {}).get("reviewed", []):
        verdict = str(r.get("verdict", "")).lower()
        sym     = r.get("symbol")
        if verdict == "veto" and sym:
            vetoed.add(sym)
            log(f"🔴 Grok VETO {sym}: {r.get('note','')[:60]} — skipping")
        elif verdict == "caution" and sym:
            log(f"🟡 Grok caution on {sym}: {r.get('note','')[:60]}")

    # ── Round 3 — Claude confirms its best trades ──────────────
    log("🔵 Round 3 — Claude confirming best trades...")
    c_review_prompt = f"""Your proposed trades: {c_trades}. Grok's risk review: {(grok_review or {}).get('reviewed', [])}.
Your budget: ${pool['claude']:.2f}. Confirm your best 1-2 trades (owner=claude).
Min $8. Confidence 80%+.
JSON: {{"refined_trades":[{{"action":"buy|sell","symbol":"NVDA","notional_usd":15.0,"confidence":85,"f":"flags","r":"<8w>","owner":"claude"}}]}}"""

    # Re-check health here (not c_ok from before Round 1) — a
    # credits_exhausted failure in Round 1 flips claude_healthy to False
    # immediately, and we don't want Round 3 to hit the same dead API.
    claude_r2 = (ask_with_retry(ask_claude_guarded, c_review_prompt,
        "You are Claude confirming your proposed trades. ONLY valid JSON under 500 chars.")
        if shared_state.get("claude_healthy", True) else None)

    if claude_r2: log(f"🔵 Claude confirmed: {len(claude_r2.get('refined_trades',[]))} trades")

    c_ref = (claude_r2 or {}).get("refined_trades", (claude_r1 or {}).get("proposed_trades",[])[:2])
    final_trades = [t for t in c_ref if t.get("symbol") not in vetoed]
    for t in final_trades:
        t["owner"] = "claude"
        t.setdefault("fee_estimate", estimate_fees(float(t.get("notional_usd", 0) or 0)))

    total_alloc = sum(t.get("notional_usd", 0) for t in final_trades)
    log(f"🎯 Final plan: {len(final_trades)} Claude trade(s), Grok-reviewed | ${total_alloc:.2f} to deploy | Cash: ${cash:.2f}")

    for t in final_trades:
        log(f"   [CLAUDE] {t.get('action','?').upper()} {t.get('symbol','?')} "
            f"${t.get('notional_usd',0):.2f} conf={t.get('confidence','?')}% "
            f"fee≈${t.get('fee_estimate',0):.3f}")

    # Update bearish watchlist
    for sym in (claude_r1 or {}).get("bearish_watchlist", []):
        if sym not in shared_state["bearish_watchlist"]:
            shared_state["bearish_watchlist"].append(sym)
    if shared_state["bearish_watchlist"]:
        log(f"📋 Bearish watchlist: {shared_state['bearish_watchlist']}")

    autonomy_unlocked = equity >= 150
    return final_trades, autonomy_unlocked, {"joint_message": f"{len(final_trades)} Claude trade(s), Grok-reviewed"}

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
