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
    "last_sleep_time":     "",
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
    "watch_mode_active":   False,
    "failover_mode":       False,
    # Crypto
    "crypto_day_start":    0.0,
    "crypto_last_day":     "",
    "crypto_last_run":     None,
    "crypto_month_start":  0.0,
    "crypto_year_start":   0.0,
    # Misc
    "boot_time":           "",
    "last_cash":           0.0,
    "last_equity":         55.0,
    "last_sync":           "",
    "last_snapshot_time":  "",
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
    except Exception:
        results["_volume_mounted"] = False
        results["_volume_contents"] = []

    return jsonify(results)

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
    try:
        _repair_scan(msg)
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
                            ask_grok_fn   = ask_grok,
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
        if symbol in restricted:
            log(f"🔒 {symbol} RESTRICTED — skipping all actions (close manually in Alpaca)")
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
            # Dynamic position sizing — scale with equity, not fixed AI suggestion
            def _dynamic_notional(eq, cash_avail, owner_budget):
                if eq < 200:
                    target = round(eq * 0.20, 2)   # 20% of equity
                elif eq < 500:
                    target = round(eq * 0.15, 2)   # 15% of equity
                elif eq < 2000:
                    target = round(eq * 0.12, 2)   # 12% of equity
                else:
                    target = min(round(eq * 0.10, 2), 500)  # 10%, cap $500
                # Never exceed cash, owner budget, or 90% of cash
                return min(target, cash_avail * 0.90, owner_budget)
            sized_notional = _dynamic_notional(equity, remaining_cash, max_for_owner)
            if sized_notional != notional:
                log(f"   📐 Position sizing: AI=${notional:.2f} → scaled=${sized_notional:.2f} (equity=${equity:.0f})")
            notional = sized_notional
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



# [classify_ai_error → moved to ai_clients.py]
# [safe_ask_claude → moved to ai_clients.py]
# [safe_ask_grok → moved to ai_clients.py]
# [check_ai_health → moved to ai_clients.py]
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

# ── Boot sequence — load persistent data from volume ─────────
# Runs after all modules imported, before trading loop starts
trade_history[:] = _load_trade_history()   # Load into existing list (keeps reference)
_load_shared_state()                        # Restore equity baselines
_load_sleep_state()                         # Restore AI sleep/wake state

# ── Late injection: sleep + PDT + portfolio (need functions defined after log()) ──
# portfolio_manager needs trade_history, alpaca, prompt_builder
_portfolio_manager._set_context(
    log_fn             = log,
    shared_state_ref   = shared_state,
    trade_history_ref  = trade_history,
    rules              = RULES,
    alpaca_fn          = alpaca,
    prompt_builder_ref = prompt_builder,
)
# Boot replay — seeds AI memory from trade history (needs portfolio_manager injected)
try:
    _replay_trade_history_into_memory()
except Exception:
    pass
# sleep_manager needs get_cash_thresholds (defined ~line 2640)
_sleep_manager._set_context(log, shared_state,
                             get_cash_thresholds_fn = get_cash_thresholds,
                             get_spy_trend_fn       = get_spy_trend,
                             save_state_fn_ref      = _save_all_persistent_state)
_pdt_manager._set_context(
    log_fn                = log,
    shared_state_ref      = shared_state,
    rules                 = RULES,
    alpaca_fn             = alpaca,
    ask_claude_fn         = ask_claude,
    ask_grok_fn           = ask_grok,
    parse_json_fn         = parse_json,
    smart_sell_fn         = smart_sell,
    record_trade_fn       = record_trade,
    get_bars_fn           = get_bars,
    compute_indicators_fn = compute_indicators,
)



# [_replay_trade_history_into_memory → portfolio_manager.py]
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

    # ── Phase 1 features ──────────────────────────────────────
    log("😱 Fetching Fear & Greed Index...")
    fear_greed = get_fear_greed_index()

    log("📅 Checking earnings calendar...")
    pos_syms_list = [p["symbol"] for p in positions] if positions else []
    earnings_warnings = get_earnings_calendar(pos_syms_list + list(RULES.get("universe", [])))

    # Append to market_ctx so AIs see it
    if fear_greed:
        fgi_val  = fear_greed.get("value", 50)
        fgi_sig  = fear_greed.get("signal", "")
        market_ctx += f"\nFear & Greed Index: {fgi_val}/100 — {fgi_sig}"
    if earnings_warnings:
        warn_str = " | ".join([f"{s}: {v['warning']}" for s, v in earnings_warnings.items()])
        market_ctx += f"\n⚠️ EARNINGS RISK: {warn_str}"
        log(f"⚠️ Earnings risk symbols: {list(earnings_warnings.keys())}")

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

    # Reset PDT intraday tracking for new day
    reset_intraday_buys_if_new_day()

    account = alpaca("GET", "/v2/account")
    equity  = float(account["equity"])

    # Reset day_start_equity each morning so daily loss limit is accurate
    shared_state["day_start_equity"] = equity
    prompt_builder._day_start_equity = equity
    try:
        _repair_reset()
    except Exception:
        pass
    # Trim trade history to rolling 6 months
    try:
        _trim_trade_history_to_6months()
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

    finally:
        if not shared_state.get("ai_sleeping", False):
            ai_sleep(reason="afterhours review complete — sleeping until 8:30am")
            log("😴 AIs sleeping after afterhours — next wake: 8:30am premarket")


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

# [ai_sleep → sleep_manager.py]
# [ai_wake → sleep_manager.py]
# [check_wake_conditions → sleep_manager.py]
# [check_ai_wake_instructions → sleep_manager.py]
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
                    shared_state["last_cash"] = current_cash

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
                            ai_sleep("premarket research done — both AIs sleeping, bot takes over")
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
                            ai_sleep("afterhours done — both AIs sleeping, bot guards overnight")
                        elif not pos_ah and cash_ah < thresh_ah["active"]:
                            ai_sleep("afterhours done — no positions, both AIs sleeping")
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
                    exits = crypto_trader.run_exit_monitor(
                        record_trade_fn = record_trade,
                        prompt_builder  = prompt_builder,
                    )
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
