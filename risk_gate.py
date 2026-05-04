"""
risk_gate.py — Unified gate for new position entries.

═══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
═══════════════════════════════════════════════════════════════════════
Borrowed from a strategy doc the user reviewed. The four rules:
  1. BTC < 200 EMA on 4H → no new entries (macro filter)
  2. -3% wallet loss in 24h → circuit breaker, no new entries until
     the next UTC day
  3. Yesterday closed negative → 24-hour cooldown (revenge-trade
     prevention)
  4. Weekend (Sat/Sun UTC) → no new entries (low liquidity)

Existing exit management (stop-losses, take-profits, trail stops)
runs UNCHANGED. This module ONLY gates new buys. If a held position
needs to exit, that always happens.

═══════════════════════════════════════════════════════════════════════
RULE 2 vs RULE 3 OVERLAP RESOLUTION
═══════════════════════════════════════════════════════════════════════
Naive implementations of these two rules can fire at the same time
for what is actually one event. We resolve this by giving each rule
a different lookback domain:
  - Rule 2 (circuit breaker): rolling-window 24-hour P&L
  - Rule 3 (post-loss cooldown): calendar-day yesterday's P&L
A bad afternoon triggers Rule 2 (rolling). A bad calendar yesterday
triggers Rule 3 (calendar). Both pause entries; the user just sees
ONE reason in the log instead of two.

═══════════════════════════════════════════════════════════════════════
ROTATIONS DURING COOLDOWN
═══════════════════════════════════════════════════════════════════════
When the gate is active, sells (incl. rotation sells) execute normally.
Buys are blocked. Rotation buys also blocked. Net effect: capital
piles up as USDT during cooldown, doesn't get redeployed. Conservative.

═══════════════════════════════════════════════════════════════════════
PERFORMANCE: 4H BTC EMA CACHE
═══════════════════════════════════════════════════════════════════════
The bot runs cycles every 5 minutes. Recomputing BTC 200 EMA on 4H
bars every cycle is wasteful. We cache the trend state and only
refresh when a new 4H bar would have closed (every 4 hours).
"""

import json
import os
from datetime import datetime, timezone, timedelta
from collections import deque

# ── Configuration ───────────────────────────────────────────
BTC_EMA_PERIOD            = 200       # 200-period EMA on 4H = institutional macro filter
BTC_EMA_TIMEFRAME_HOURS   = 4         # 4-hour candles
BTC_EMA_CACHE_TTL_SECONDS = 4 * 3600  # Refresh once per 4-hour bar
BTC_EMA_BARS_NEEDED       = 250       # Need ~250 bars for stable 200 EMA

DAILY_LOSS_PCT_THRESHOLD  = 0.03      # -3% of wallet in 24h → circuit breaker
ROLLING_WINDOW_HOURS      = 24        # Window for Rule 2

POST_LOSS_COOLDOWN_HOURS  = 24        # Rule 3: skip entries 24h after a losing day

# Toggles — for emergency disable
ENABLE_MACRO_FILTER     = True
ENABLE_CIRCUIT_BREAKER  = True
ENABLE_POST_LOSS_COOL   = True
ENABLE_WEEKEND_PAUSE    = True

STATE_FILE          = "/data/risk_gate.json"
FALLBACK_STATE_FILE = "./risk_gate.json"

# ── Injected dependencies ───────────────────────────────────
log = print
get_btc_4h_bars  = None    # Returns list of 4H bars [(open_ts, o, h, l, c, v), ...]
load_trade_hist  = None    # Returns list of bot trades
get_wallet_value = None    # Returns float — current total wallet


def _set_context(log_fn=None, btc_bars_fn=None,
                 trade_history_fn=None, wallet_fn=None):
    global log, get_btc_4h_bars, load_trade_hist, get_wallet_value
    if log_fn:           log = log_fn
    if btc_bars_fn:      get_btc_4h_bars = btc_bars_fn
    if trade_history_fn: load_trade_hist = trade_history_fn
    if wallet_fn:        get_wallet_value = wallet_fn


# ── State management ────────────────────────────────────────
def _empty_state():
    return {
        "btc_macro": {
            "last_check_ts":  None,
            "btc_price":      0.0,
            "btc_200ema":     0.0,
            "trend":          "UNKNOWN",   # BULLISH / BEARISH / UNKNOWN
        },
        "last_check_ts":      None,
        "last_block_reason":  None,
        "block_count_24h":    0,
    }


def _load_state() -> dict:
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path) as f:
                state = json.load(f)
                # Schema migrations
                defaults = _empty_state()
                for k, v in defaults.items():
                    if k not in state:
                        state[k] = v
                return state
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return _empty_state()


def _save_state(state: dict):
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2, default=str)
            return True
        except Exception:
            continue
    return False


# ── EMA computation ─────────────────────────────────────────
def _compute_ema(values: list, period: int) -> float:
    """Standard EMA. Returns last EMA value."""
    if len(values) < period:
        return 0.0
    multiplier = 2.0 / (period + 1)
    # Seed with SMA of first `period` values
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


# ── Rule 1: BTC macro filter ────────────────────────────────
def _btc_macro_check(state: dict) -> tuple:
    """
    Returns (allowed, reason).
    Cached: only recomputes EMA once per 4 hours.
    """
    if not ENABLE_MACRO_FILTER:
        return True, ""
    if not get_btc_4h_bars:
        # No data source wired — fail-open (don't block on missing infra)
        return True, ""

    cache = state.get("btc_macro", {})
    now_ts = datetime.now(timezone.utc).timestamp()
    last_ts = cache.get("last_check_ts") or 0
    if (now_ts - last_ts) < BTC_EMA_CACHE_TTL_SECONDS and cache.get("trend") in ("BULLISH", "BEARISH"):
        # Use cached value
        if cache["trend"] == "BULLISH":
            return True, ""
        return False, (f"macro: BTC ${cache.get('btc_price', 0):.0f} below 200-EMA "
                       f"${cache.get('btc_200ema', 0):.0f} on 4H (cached)")

    # Refresh
    try:
        bars = get_btc_4h_bars(limit=BTC_EMA_BARS_NEEDED) or []
        closes = [float(b.get("c", b.get("close", 0))) for b in bars if b]
        closes = [c for c in closes if c > 0]
        if len(closes) < BTC_EMA_PERIOD + 10:
            log(f"   ⚠️ risk_gate: only {len(closes)} BTC 4H bars available — "
                f"skipping macro filter (need {BTC_EMA_PERIOD + 10}+)")
            return True, ""
        ema = _compute_ema(closes, BTC_EMA_PERIOD)
        price_now = closes[-1]
        trend = "BULLISH" if price_now >= ema else "BEARISH"
        cache.update({
            "last_check_ts": now_ts,
            "btc_price":     round(price_now, 2),
            "btc_200ema":    round(ema, 2),
            "trend":         trend,
        })
        state["btc_macro"] = cache
        if trend == "BULLISH":
            return True, ""
        return False, (f"macro: BTC ${price_now:.0f} below 200-EMA ${ema:.0f} on 4H")
    except Exception as e:
        log(f"   ⚠️ risk_gate: BTC macro check failed: {e} — fail-open")
        return True, ""


# ── Rule 4: Weekend pause ───────────────────────────────────
def _weekend_check() -> tuple:
    if not ENABLE_WEEKEND_PAUSE:
        return True, ""
    now = datetime.now(timezone.utc)
    # weekday(): Monday=0 ... Sunday=6
    if now.weekday() in (5, 6):    # Saturday or Sunday
        return False, f"weekend: Sat/Sun UTC liquidity paused for new entries"
    return True, ""


# ── Trade history helpers ───────────────────────────────────
def _trades_with_pnl(trades: list) -> list:
    """Filter trade history to closed trades that have realized P&L."""
    if not trades:
        return []
    closed = []
    for t in trades:
        # Various schemas in use across the bot's trade history
        pnl = t.get("realized_pnl_usd")
        if pnl is None:
            pnl = t.get("pnl_usd")
        if pnl is None:
            pnl = t.get("pnl")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (ValueError, TypeError):
            continue
        ts_ms = t.get("time_ms") or t.get("timestamp_ms") or 0
        if not ts_ms:
            ts_str = t.get("timestamp") or t.get("time") or ""
            if ts_str:
                try:
                    ts_ms = int(datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    continue
        if ts_ms <= 0:
            continue
        # Only count actual closes (not buys, not partial fills)
        action = (t.get("action") or t.get("side") or "").lower()
        if action in ("buy", "open"):
            continue
        closed.append({"ts_ms": ts_ms, "pnl": pnl, "symbol": t.get("symbol", "")})
    return closed


# ── Rule 2: 24h circuit breaker ─────────────────────────────
def _circuit_breaker_check() -> tuple:
    if not ENABLE_CIRCUIT_BREAKER:
        return True, ""
    if not load_trade_hist or not get_wallet_value:
        return True, ""    # fail-open if not wired

    try:
        trades = load_trade_hist() or []
        closes = _trades_with_pnl(trades)
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=ROLLING_WINDOW_HOURS)).timestamp() * 1000)
        recent = [c for c in closes if c["ts_ms"] >= cutoff_ms]
        if not recent:
            return True, ""
        pnl_24h = sum(c["pnl"] for c in recent)
        wallet  = float(get_wallet_value() or 0)
        if wallet <= 0:
            return True, ""
        loss_pct = pnl_24h / wallet
        if loss_pct <= -DAILY_LOSS_PCT_THRESHOLD:
            return False, (f"circuit breaker: 24h P&L ${pnl_24h:+.2f} "
                           f"({loss_pct*100:+.1f}% of ${wallet:.2f} wallet) — "
                           f"threshold -{DAILY_LOSS_PCT_THRESHOLD*100:.0f}%")
        return True, ""
    except Exception as e:
        log(f"   ⚠️ risk_gate: circuit breaker check failed: {e} — fail-open")
        return True, ""


# ── Rule 3: Post-loss cooldown ──────────────────────────────
def _post_loss_cooldown_check() -> tuple:
    """
    If the most recent CALENDAR DAY (UTC) ended negative, pause new
    entries for COOLDOWN hours after that day's last close.
    Distinct from circuit breaker: this watches calendar boundaries,
    not rolling windows.
    """
    if not ENABLE_POST_LOSS_COOL:
        return True, ""
    if not load_trade_hist:
        return True, ""

    try:
        trades = load_trade_hist() or []
        closes = _trades_with_pnl(trades)
        if not closes:
            return True, ""

        # Group by UTC calendar date
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        by_day = {}
        for c in closes:
            day = datetime.fromtimestamp(c["ts_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(c)

        # Find the most recent COMPLETED day (i.e., not today)
        prior_days = sorted([d for d in by_day if d < today_str], reverse=True)
        if not prior_days:
            return True, ""
        last_day = prior_days[0]
        last_day_pnl = sum(c["pnl"] for c in by_day[last_day])
        if last_day_pnl >= 0:
            return True, ""    # Last completed day was profitable — no cooldown

        # Last completed day was negative. Check whether 24h has passed
        # since the LAST close of that day.
        last_close_ts = max(c["ts_ms"] for c in by_day[last_day])
        hours_since = (now.timestamp() * 1000 - last_close_ts) / (1000 * 3600)
        if hours_since >= POST_LOSS_COOLDOWN_HOURS:
            return True, ""    # Cooldown elapsed
        remaining = POST_LOSS_COOLDOWN_HOURS - hours_since
        return False, (f"post-loss cooldown: {last_day} closed ${last_day_pnl:+.2f} — "
                       f"{remaining:.1f}h remaining in cooldown")
    except Exception as e:
        log(f"   ⚠️ risk_gate: cooldown check failed: {e} — fail-open")
        return True, ""


# ── Public API ──────────────────────────────────────────────
def can_open_new_positions(silent: bool = False) -> tuple:
    """
    Single decision point: should the bot accept ANY new buy proposal?
    Returns (allowed: bool, reason: str). Reason is empty when allowed.
    Does NOT affect exit management — sells/stops/TPs always run.
    """
    state = _load_state()
    state["last_check_ts"] = datetime.now(timezone.utc).isoformat()

    # Check rules in priority order. First failure wins.
    for check_name, check_fn in [
        ("macro",           lambda: _btc_macro_check(state)),
        ("weekend",         _weekend_check),
        ("circuit_breaker", _circuit_breaker_check),
        ("post_loss",       _post_loss_cooldown_check),
    ]:
        try:
            allowed, reason = check_fn()
        except Exception as e:
            log(f"   ⚠️ risk_gate: {check_name} crashed: {e} — fail-open")
            continue
        if not allowed:
            state["last_block_reason"] = reason
            _save_state(state)
            if not silent:
                log(f"   🛑 RISK GATE BLOCKED: {reason}")
            return False, reason

    state["last_block_reason"] = None
    _save_state(state)
    return True, ""


def get_status() -> dict:
    """Snapshot for /risk_gate endpoint."""
    state = _load_state()
    allowed, reason = can_open_new_positions(silent=True)
    return {
        "allowed":           allowed,
        "block_reason":      reason if not allowed else None,
        "rules_enabled": {
            "macro_filter":      ENABLE_MACRO_FILTER,
            "circuit_breaker":   ENABLE_CIRCUIT_BREAKER,
            "post_loss_cooldown": ENABLE_POST_LOSS_COOL,
            "weekend_pause":     ENABLE_WEEKEND_PAUSE,
        },
        "thresholds": {
            "daily_loss_pct":            DAILY_LOSS_PCT_THRESHOLD,
            "rolling_window_hours":      ROLLING_WINDOW_HOURS,
            "post_loss_cooldown_hours":  POST_LOSS_COOLDOWN_HOURS,
        },
        "btc_macro_state":    state.get("btc_macro", {}),
        "last_check_ts":      state.get("last_check_ts"),
        "last_block_reason":  state.get("last_block_reason"),
    }


def format_for_ai_prompt() -> str:
    """
    Compact line for AI prompt context. AIs need to know when entries
    are gated so they understand why their proposals might be skipped.
    Returns empty string if no relevant context.
    """
    state = _load_state()
    btc = state.get("btc_macro", {})
    parts = []
    if btc.get("trend") == "BEARISH":
        parts.append(f"⛔ BTC BEARISH on 4H (${btc.get('btc_price', 0):.0f} < EMA200 ${btc.get('btc_200ema', 0):.0f}) — entries paused")
    elif btc.get("trend") == "BULLISH":
        parts.append(f"✅ BTC bullish on 4H (price > 200 EMA)")
    last_block = state.get("last_block_reason")
    if last_block and "macro" not in last_block.lower():
        parts.append(f"⚠️ Risk gate active: {last_block}")
    if not parts:
        return ""
    return "🛡️ RISK GATE: " + " | ".join(parts)
