"""
coin_performance.py — Per-coin trading performance memory.

═══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
═══════════════════════════════════════════════════════════════════════
The AIs need concrete, evidence-based feedback about which coins they've
been winning and losing on. The lessons memory in prompt_builder.py is
abstract — "you've had 5 wins" isn't actionable. This module gives the
AIs hard per-coin numbers and classifies each coin so they know when
to back off.

Specifically: the bot lost ~$25 in a week on coins like KAVA (14 trades
0 wins -$9.27) and AUDIO (4 trades 0 wins -$10.64) because the AIs kept
proposing the same losing setups without seeing the cumulative damage.

This module:
  1. Reads /data/binance_trade_history.json (the synced fill history)
  2. Runs FIFO matching to compute realized P&L per coin
  3. Computes both LIFETIME and ROLLING 14-day stats
  4. Analyzes TP hit rate vs stop hit rate (regime detection signal)
  5. Classifies each coin: AVOID / CAUTION / NEUTRAL / PROVEN
  6. Persists to /data/coin_performance.json (survives redeploys)
  7. Exposes a compact text block for AI prompts

═══════════════════════════════════════════════════════════════════════
CLASSIFICATION RULES (rolling 14-day window)
═══════════════════════════════════════════════════════════════════════
  AVOID    → 5+ trades AND 0 wins AND P&L <= -$5
             AI is told "do not propose trades on this coin"
  CAUTION  → 3+ trades AND win_rate <33%
             AI is told "needs strong conviction (high confidence)"
  PROVEN   → 5+ trades AND win_rate >=60%
             AI is told "this coin has been working — bias slightly toward it"
  NEUTRAL  → all other cases (including insufficient data)

═══════════════════════════════════════════════════════════════════════
MANUAL OVERRIDES
═══════════════════════════════════════════════════════════════════════
The user can set a manual override per coin via /coin_performance/override
endpoint. Manual settings always win over auto-classification. Use cases:
  - "I know KAVA is recovering — override AVOID to NEUTRAL"
  - "I never want to trade DOGE — force AVOID permanently"

═══════════════════════════════════════════════════════════════════════
TP HIT RATE (global, not per-coin)
═══════════════════════════════════════════════════════════════════════
For each completed sell, we estimate whether the exit was a TP hit or
a stop hit by checking the realized %:
  - Realized > +5%  → TP hit (or close)
  - Realized < -2%  → Stop hit
  - In between      → Neutral exit

Low TP hit rate across the universe is a regime signal: market isn't
supporting the configured TP target, suggesting tighter TPs or higher
confidence thresholds.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Configuration ───────────────────────────────────────────
ROLLING_WINDOW_DAYS = 14

AVOID_MIN_TRADES   = 5
AVOID_MIN_LOSS_USD = 5.00
AVOID_MAX_WINS     = 0      # zero wins required to AVOID

CAUTION_MIN_TRADES = 3
CAUTION_MAX_WR     = 0.33

PROVEN_MIN_TRADES  = 5
PROVEN_MIN_WR      = 0.60

# TP/Stop hit detection thresholds (as % of entry)
TP_HIT_THRESHOLD_PCT   = 5.0    # >+5% → counted as TP hit
STOP_HIT_THRESHOLD_PCT = -2.0   # <-2% → counted as stop hit

STATE_FILE = "/data/coin_performance.json"
FALLBACK_STATE_FILE = "./coin_performance.json"

# ── Injected dependencies ───────────────────────────────────
log = print
load_binance_history_fn = None     # bot's _load_binance_history()


def _set_context(log_fn=None, history_loader=None):
    global log, load_binance_history_fn
    if log_fn:           log = log_fn
    if history_loader:   load_binance_history_fn = history_loader


# ── State management ────────────────────────────────────────
def _empty_state():
    return {
        "generated_at":   None,
        "trade_count":    0,
        "coins":          {},
        "global":         {},
        "manual_overrides": {},
    }


def _load_state() -> dict:
    """Load persisted report (if exists). Returns empty state on miss."""
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            try: log(f"⚠️ coin_performance load failed at {path}: {e}")
            except Exception: pass
    return _empty_state()


def _save_state(state: dict) -> bool:
    """Persist report to /data/."""
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2, default=str)
            return True
        except Exception:
            continue
    return False


# ── Core computation ────────────────────────────────────────
def _classify(stats: dict, manual_override: str = None) -> tuple:
    """
    Classify a coin based on rolling 14-day stats.
    Returns (status, reason_string).
    Manual override always wins.
    """
    if manual_override and manual_override in ("AVOID", "CAUTION", "NEUTRAL", "PROVEN"):
        return manual_override, "manual override"

    trades = stats.get("trades", 0)
    wins   = stats.get("wins",   0)
    pnl    = stats.get("realized_pnl_usd", 0)
    wr     = (wins / trades) if trades > 0 else 0

    # AVOID conditions (most restrictive — checked first)
    if trades >= AVOID_MIN_TRADES and wins <= AVOID_MAX_WINS and pnl <= -AVOID_MIN_LOSS_USD:
        return "AVOID", f"{wins}/{trades} wins, ${pnl:+.2f} in last {ROLLING_WINDOW_DAYS}d"

    # PROVEN conditions
    if trades >= PROVEN_MIN_TRADES and wr >= PROVEN_MIN_WR:
        return "PROVEN", f"{wins}/{trades} wins ({wr*100:.0f}%), ${pnl:+.2f}"

    # CAUTION conditions
    if trades >= CAUTION_MIN_TRADES and wr < CAUTION_MAX_WR:
        return "CAUTION", f"{wins}/{trades} wins ({wr*100:.0f}%), ${pnl:+.2f}"

    return "NEUTRAL", f"{wins}/{trades} trades, ${pnl:+.2f}"


def _classify_exit(realized_pct: float) -> str:
    """Classify a sell exit as 'tp_hit', 'stop_hit', or 'neutral'."""
    if realized_pct >= TP_HIT_THRESHOLD_PCT:
        return "tp_hit"
    if realized_pct <= STOP_HIT_THRESHOLD_PCT:
        return "stop_hit"
    return "neutral"


def _compute_fifo_pnl(trades: list, since_ts_ms: int = 0) -> dict:
    """
    Run FIFO matching across all trades. Optional since_ts_ms filters
    to only count CLOSES that happened after that timestamp (window stat).
    Buys before the window are still used as cost basis.

    Returns: {symbol: {trades, wins, losses, realized_pnl_usd,
                       tp_hits, stop_hits, neutral_hits,
                       first_close_ts, last_close_ts,
                       incomplete_trades}}
    """
    queues = defaultdict(list)   # symbol → [[qty, price, time_ms], ...]
    stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "realized_pnl_usd": 0.0,
        "tp_hits": 0, "stop_hits": 0, "neutral_hits": 0,
        "first_close_ts": None, "last_close_ts": None,
        "incomplete_trades": 0,    # sells with no matching buy
    })

    sorted_trades = sorted(trades, key=lambda t: t.get("time_ms", 0))

    for t in sorted_trades:
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        side = (t.get("side") or "").lower()
        qty   = float(t.get("qty",   0) or 0)
        price = float(t.get("price", 0) or 0)
        ts_ms = int(t.get("time_ms", 0) or 0)
        if qty <= 0 or price <= 0:
            continue

        if side == "buy":
            queues[sym].append([qty, price, ts_ms])
            continue

        # SELL — match against FIFO queue
        sell_qty = qty
        weighted_entry_total = 0.0
        matched_qty          = 0.0
        had_match            = False
        while sell_qty > 0 and queues[sym]:
            had_match = True
            buy_qty, buy_price, _ = queues[sym][0]
            take = min(sell_qty, buy_qty)
            weighted_entry_total += buy_price * take
            matched_qty          += take
            sell_qty             -= take
            queues[sym][0][0]    -= take
            if queues[sym][0][0] <= 0.000001:
                queues[sym].pop(0)

        # Only count this CLOSE if it happened in the window
        if ts_ms < since_ts_ms:
            continue

        # If we couldn't match anything, skip — don't pollute stats
        # with sells against unknown cost basis (causes false negatives
        # like FET showing 0 wins when April 23 sell was vs pre-April-1 buy)
        if not had_match or matched_qty <= 0:
            stats[sym]["incomplete_trades"] += 1
            continue

        avg_entry = weighted_entry_total / matched_qty
        realized_pct  = ((price - avg_entry) / avg_entry) * 100 if avg_entry else 0
        realized_usd  = (price - avg_entry) * matched_qty

        # Skip dust sells (P&L < $0.01) — they distort stats
        if abs(realized_usd) < 0.01:
            continue

        s = stats[sym]
        s["trades"]           += 1
        s["realized_pnl_usd"] += realized_usd
        if realized_usd > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        # Exit classification — increment the matching counter
        exit_type = _classify_exit(realized_pct)
        if exit_type == "tp_hit":
            s["tp_hits"] += 1
        elif exit_type == "stop_hit":
            s["stop_hits"] += 1
        else:
            s["neutral_hits"] += 1
        # Time stamps
        if s["first_close_ts"] is None or ts_ms < s["first_close_ts"]:
            s["first_close_ts"] = ts_ms
        if s["last_close_ts"] is None or ts_ms > s["last_close_ts"]:
            s["last_close_ts"] = ts_ms

    # Round everything for clean JSON
    for sym, s in stats.items():
        s["realized_pnl_usd"] = round(s["realized_pnl_usd"], 2)
    return dict(stats)


def _compute_open_positions(trades: list) -> dict:
    """After full FIFO replay, what's left in the queues = held positions."""
    queues = defaultdict(list)
    sorted_trades = sorted(trades, key=lambda t: t.get("time_ms", 0))
    for t in sorted_trades:
        sym = (t.get("symbol") or "").upper()
        side = (t.get("side") or "").lower()
        qty = float(t.get("qty", 0) or 0)
        price = float(t.get("price", 0) or 0)
        if qty <= 0 or price <= 0 or not sym:
            continue
        if side == "buy":
            queues[sym].append([qty, price])
        else:
            sell_qty = qty
            while sell_qty > 0 and queues[sym]:
                buy_qty, buy_price = queues[sym][0]
                take = min(sell_qty, buy_qty)
                sell_qty -= take
                queues[sym][0][0] -= take
                if queues[sym][0][0] <= 0.000001:
                    queues[sym].pop(0)

    open_positions = {}
    for sym, q in queues.items():
        if not q:
            continue
        total_qty = sum(item[0] for item in q)
        if total_qty <= 0.000001:
            continue
        avg_cost = sum(item[0] * item[1] for item in q) / total_qty
        open_positions[sym] = {
            "qty":         round(total_qty, 8),
            "avg_cost":    round(avg_cost, 8),
            "cost_basis":  round(total_qty * avg_cost, 2),
        }
    return open_positions


def regenerate(force: bool = False) -> dict:
    """
    Read the Binance trade history, recompute all stats, save report.
    Returns the new report dict.
    """
    if not load_binance_history_fn:
        log("⚠️ coin_performance: history loader not wired — skipping")
        return _load_state()

    try:
        trades = load_binance_history_fn() or []
    except Exception as e:
        log(f"⚠️ coin_performance: history load failed: {e}")
        return _load_state()

    if not trades:
        log("📊 coin_performance: no trades in history yet")
        report = _empty_state()
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(report)
        return report

    # Compute lifetime + window stats
    lifetime = _compute_fifo_pnl(trades, since_ts_ms=0)
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)).timestamp() * 1000)
    window = _compute_fifo_pnl(trades, since_ts_ms=cutoff_ms)
    open_positions = _compute_open_positions(trades)

    # Pull existing manual overrides forward
    prior = _load_state()
    overrides = prior.get("manual_overrides", {})

    # Build per-coin records
    coins = {}
    all_symbols = set(lifetime.keys()) | set(window.keys()) | set(open_positions.keys())
    for sym in all_symbols:
        lt = lifetime.get(sym, {})
        wd = window.get(sym, {})
        op = open_positions.get(sym)
        manual = overrides.get(sym)

        status, reason = _classify(wd, manual_override=manual)

        coins[sym] = {
            "lifetime":  {
                "trades": lt.get("trades", 0),
                "wins":   lt.get("wins", 0),
                "losses": lt.get("losses", 0),
                "win_rate": round(lt.get("wins", 0) / max(lt.get("trades", 1), 1), 3) if lt.get("trades", 0) > 0 else 0,
                "realized_pnl_usd":  lt.get("realized_pnl_usd", 0),
                "incomplete_trades": lt.get("incomplete_trades", 0),
            },
            f"window_{ROLLING_WINDOW_DAYS}d": {
                "trades": wd.get("trades", 0),
                "wins":   wd.get("wins", 0),
                "losses": wd.get("losses", 0),
                "win_rate": round(wd.get("wins", 0) / max(wd.get("trades", 1), 1), 3) if wd.get("trades", 0) > 0 else 0,
                "realized_pnl_usd":  wd.get("realized_pnl_usd", 0),
                "tp_hits":   wd.get("tp_hits", 0),
                "stop_hits": wd.get("stop_hits", 0),
                "neutral_hits": wd.get("neutral_hits", 0),
            },
            "current_position": op,
            "status":           status,
            "reason":           reason,
            "manual_override":  manual,
        }

    # Global TP analysis (window only — lifetime would be misleading on regime change)
    total_wins   = sum(c[f"window_{ROLLING_WINDOW_DAYS}d"]["wins"]      for c in coins.values())
    total_losses = sum(c[f"window_{ROLLING_WINDOW_DAYS}d"]["losses"]    for c in coins.values())
    total_trades = total_wins + total_losses
    total_tp     = sum(c[f"window_{ROLLING_WINDOW_DAYS}d"]["tp_hits"]   for c in coins.values())
    total_stop   = sum(c[f"window_{ROLLING_WINDOW_DAYS}d"]["stop_hits"] for c in coins.values())
    total_pnl    = sum(c[f"window_{ROLLING_WINDOW_DAYS}d"]["realized_pnl_usd"] for c in coins.values())

    tp_hit_rate   = (total_tp   / total_trades) if total_trades > 0 else 0
    stop_hit_rate = (total_stop / total_trades) if total_trades > 0 else 0

    # Diagnostic implication
    if total_trades < 5:
        implication = "Insufficient data for regime analysis."
    elif tp_hit_rate >= 0.50:
        implication = "TPs hitting reliably — current targets are well-calibrated."
    elif tp_hit_rate >= 0.30:
        implication = "Mixed — TPs hit sometimes; consider raising confidence threshold."
    elif stop_hit_rate >= 0.50:
        implication = "Stops hit more than TPs — choppy market or entries too late. Consider tighter TPs or higher confidence."
    else:
        implication = "Most exits neutral — entries lack edge. Review setup quality."

    global_stats = {
        "window_trades":   total_trades,
        "window_wins":     total_wins,
        "window_losses":   total_losses,
        "window_win_rate": round(total_wins / max(total_trades, 1), 3) if total_trades > 0 else 0,
        "window_pnl_usd":  round(total_pnl, 2),
        "tp_hit_rate":     round(tp_hit_rate, 3),
        "stop_hit_rate":   round(stop_hit_rate, 3),
        "implication":     implication,
    }

    report = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "trade_count":      len(trades),
        "rolling_window_days": ROLLING_WINDOW_DAYS,
        "coins":            coins,
        "global":           global_stats,
        "manual_overrides": overrides,
    }
    _save_state(report)

    # Compact summary log
    n_avoid   = sum(1 for c in coins.values() if c["status"] == "AVOID")
    n_caution = sum(1 for c in coins.values() if c["status"] == "CAUTION")
    n_proven  = sum(1 for c in coins.values() if c["status"] == "PROVEN")
    log(f"📊 Coin performance updated: {len(coins)} coins, "
        f"{n_avoid} AVOID, {n_caution} CAUTION, {n_proven} PROVEN | "
        f"{ROLLING_WINDOW_DAYS}d P&L: ${total_pnl:+.2f} ({total_wins}W/{total_losses}L)")
    return report


# ── Manual override management ──────────────────────────────
def set_manual_override(symbol: str, status: str) -> bool:
    """Set a manual classification for a coin. Use AVOID, CAUTION, NEUTRAL, PROVEN, or None to clear."""
    symbol = symbol.upper()
    if status and status not in ("AVOID", "CAUTION", "NEUTRAL", "PROVEN"):
        return False
    state = _load_state()
    if "manual_overrides" not in state:
        state["manual_overrides"] = {}
    if status is None:
        state["manual_overrides"].pop(symbol, None)
    else:
        state["manual_overrides"][symbol] = status
    _save_state(state)
    log(f"📊 Manual override set: {symbol} → {status or 'CLEARED'}")
    return True


# ── AI prompt formatting ────────────────────────────────────
def format_for_ai_prompt(max_per_bucket: int = 5) -> str:
    """
    Compact ~80-150 token block for injection into AI cycle prompts.
    Shows AVOID coins prominently, CAUTION next, and global TP analysis.
    Returns empty string if no meaningful data yet.
    """
    state = _load_state()
    coins = state.get("coins", {})
    if not coins:
        return ""

    avoid   = []
    caution = []
    proven  = []
    for sym, c in coins.items():
        wd = c.get(f"window_{ROLLING_WINDOW_DAYS}d", {})
        pnl = wd.get("realized_pnl_usd", 0)
        wins = wd.get("wins", 0)
        trades = wd.get("trades", 0)
        sym_short = sym.replace("USDT", "")
        if c["status"] == "AVOID":
            avoid.append(f"{sym_short} (${pnl:+.2f}, {wins}/{trades})")
        elif c["status"] == "CAUTION":
            caution.append(f"{sym_short} ({wins}/{trades}, ${pnl:+.2f})")
        elif c["status"] == "PROVEN":
            proven.append(f"{sym_short} ({wins}/{trades})")

    lines = []
    if avoid:
        lines.append(f"⛔ AVOID (do not propose): {', '.join(avoid[:max_per_bucket])}")
    if caution:
        lines.append(f"⚠️ CAUTION (need high confidence): {', '.join(caution[:max_per_bucket])}")
    if proven:
        lines.append(f"✅ WORKING: {', '.join(proven[:max_per_bucket])}")

    g = state.get("global", {})
    if g.get("window_trades", 0) >= 5:
        tp_pct = int(g.get("tp_hit_rate", 0) * 100)
        stop_pct = int(g.get("stop_hit_rate", 0) * 100)
        lines.append(f"📈 TP hit rate: {tp_pct}% | Stop rate: {stop_pct}% | {g.get('implication', '')}")

    if not lines:
        return ""
    return "📊 YOUR PERFORMANCE (last 14d):\n" + "\n".join(lines)


def get_status() -> dict:
    """For the /coin_performance endpoint."""
    return _load_state()
