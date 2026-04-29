"""
core_reserve.py — Long-term wealth compounder, walled off from tactical AIs.

═══════════════════════════════════════════════════════════════════════
DESIGN PHILOSOPHY
═══════════════════════════════════════════════════════════════════════
The tactical AIs (Claude, Grok) trade fast — minutes to hours. They get
the trading pool and compete on it. The Core Reserve is the OPPOSITE:
slow, boring, compounding capital that the AIs cannot see or touch.

But "locked" doesn't mean "passive." A 50% BTC drawdown that we sleep
through would be malpractice when we have monitoring infrastructure
already running. So the reserve has a separate watcher that fires on
4 specific contingencies — protecting downside, capturing extreme
opportunities, locking partial gains, and rebalancing drift.

═══════════════════════════════════════════════════════════════════════
ALLOCATION
═══════════════════════════════════════════════════════════════════════
• 50% BTC      — digital gold, decade-long uptrend, on Binance.US
• 30% SPY      — S&P 500 ETF, ~10% historical CAGR, on Alpaca
• 20% USDT     — true emergency liquidity + dry powder for opportunities

═══════════════════════════════════════════════════════════════════════
ACTIVATION
═══════════════════════════════════════════════════════════════════════
Activates when combined_wallet >= $1000. Tracks via state file:
  /data/core_reserve.json — ATH, entry prices, last trim/buy times

═══════════════════════════════════════════════════════════════════════
CONTINGENCY TRIGGERS (run hourly, rule-based, no AI calls)
═══════════════════════════════════════════════════════════════════════
1. DEFENSIVE TRIM
   - BTC drops >20% from entry over any 7-day window → sell 30%
   - SPY drops >15% from entry over any 7-day window → sell 30%
   - Cooldown: 72h (avoid trimming on chop)

2. OPPORTUNITY BUY
   - BTC drops >30% from ATH AND RSI < 30 → spend 50% of cash slice
   - SPY drops >20% from ATH AND RSI < 30 → spend 50% of cash slice
   - Cooldown: 168h (one big buy per week max)

3. TAKE PROFIT TRIM
   - BTC up >50% from entry → trim 20% to cash slice
   - SPY up >30% from entry → trim 20% to cash slice
   - Cooldown: 168h

4. DRIFT REBALANCE
   - Any slice deviates >15% from target → rebalance to target
   - Runs at most once per 30 days

═══════════════════════════════════════════════════════════════════════
"""
import json
import os
from datetime import datetime, timezone, timedelta
from collections import deque

# ── Configuration ────────────────────────────────────────────
ENABLE_CORE_RESERVE = True

ACTIVATION_THRESHOLD = 1000.0   # Combined wallet must reach this to activate

# Target allocation (must sum to 1.0)
TARGET_ALLOCATION = {
    "BTC":  0.50,    # Bitcoin slice (Binance.US)
    "SPY":  0.30,    # S&P 500 ETF (Alpaca)
    "USDT": 0.20,    # Cash / emergency liquidity (Binance.US)
}

# Contingency thresholds
DEFENSIVE_TRIM_BTC_DD       = 0.20   # 20% drop from entry over 7d
DEFENSIVE_TRIM_SPY_DD       = 0.15   # 15% drop (stocks more volatile floor)
DEFENSIVE_TRIM_PCT          = 0.30   # Sell 30% of position

OPPORTUNITY_BUY_BTC_DD      = 0.30   # 30% drop from ATH
OPPORTUNITY_BUY_SPY_DD      = 0.20   # 20% drop from ATH
OPPORTUNITY_BUY_RSI_MAX     = 30     # Daily RSI must confirm oversold
OPPORTUNITY_BUY_PCT_OF_CASH = 0.50   # Use 50% of cash slice

TAKE_PROFIT_BTC_GAIN        = 0.50   # +50% from entry
TAKE_PROFIT_SPY_GAIN        = 0.30   # +30% from entry
TAKE_PROFIT_TRIM_PCT        = 0.20   # Trim 20%

REBALANCE_DRIFT_THRESHOLD   = 0.15   # 15% deviation from target

# Cooldowns (hours)
COOLDOWN_DEFENSIVE   = 72
COOLDOWN_OPPORTUNITY = 168     # 1 week
COOLDOWN_TAKEPROFIT  = 168
COOLDOWN_REBALANCE   = 720     # 30 days

# Persistent state file
STATE_FILE = "/data/core_reserve.json"
FALLBACK_STATE_FILE = "./core_reserve.json"

# 7-day price history cap (1 reading per hour = 168 readings)
PRICE_HISTORY_LEN = 200

# ── Injected dependencies (set by bot on boot) ──────────────
log = print
binance_get  = None       # binance_crypto.binance_get
binance_post = None       # binance_crypto.binance_post (for orders)
alpaca       = None       # bot's alpaca() function
get_full_wallet = None    # binance_crypto.get_full_wallet
record_trade = None       # bot's record_trade

# Stock symbol fallback chain (in case SPY isn't tradeable for some reason)
EQUITY_TICKER = "SPY"    # The actual ticker we'll buy on Alpaca


# ──────────────────────────────────────────────────────────────
def _set_context(log_fn=None, binance_get_fn=None, binance_post_fn=None,
                 alpaca_fn=None, wallet_fn=None, record_trade_fn=None):
    """Inject runtime dependencies. Called once on boot."""
    global log, binance_get, binance_post, alpaca, get_full_wallet, record_trade
    if log_fn:           log = log_fn
    if binance_get_fn:   binance_get = binance_get_fn
    if binance_post_fn:  binance_post = binance_post_fn
    if alpaca_fn:        alpaca = alpaca_fn
    if wallet_fn:        get_full_wallet = wallet_fn
    if record_trade_fn:  record_trade = record_trade_fn


# ──────────────────────────────────────────────────────────────
# STATE (persists across redeploys via /data volume)
# ──────────────────────────────────────────────────────────────
def _default_state():
    """Empty state dict for fresh boot."""
    return {
        "activated":         False,
        "activated_at":      None,
        # Per-slice tracking
        "btc": {
            "qty":            0.0,
            "entry_price":    0.0,        # Volume-weighted avg cost basis
            "ath_price":      0.0,        # All-time high since activation
            "last_defensive": None,        # ISO timestamp
            "last_opportunity": None,
            "last_takeprofit":  None,
            "total_invested":   0.0,       # USD ever put in (for true P&L)
        },
        "spy": {
            "qty":            0.0,
            "entry_price":    0.0,
            "ath_price":      0.0,
            "last_defensive": None,
            "last_opportunity": None,
            "last_takeprofit":  None,
            "total_invested":   0.0,
        },
        "cash_usdt":        0.0,           # The 20% cash slice tracker
        # Meta
        "last_rebalance":     None,
        "last_check":         None,
        "total_contributions":0.0,         # Cumulative dollars deposited
        # 7-day rolling price windows for drawdown detection
        "btc_price_history":  [],          # list of [iso_ts, price]
        "spy_price_history":  [],
        # Event log (append-only audit trail)
        "events":             [],
    }


_state = None  # Lazy-loaded


def _load_state():
    """Load core reserve state from persistent volume."""
    global _state
    if _state is not None:
        return _state
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path) as f:
                _state = json.load(f)
                # Guarantee all keys exist (handles schema additions)
                _default = _default_state()
                for k, v in _default.items():
                    if k not in _state:
                        _state[k] = v
                # Same for nested dicts
                for slice_name in ("btc", "spy"):
                    for sub_k, sub_v in _default[slice_name].items():
                        if sub_k not in _state[slice_name]:
                            _state[slice_name][sub_k] = sub_v
                return _state
        except FileNotFoundError:
            continue
        except Exception as e:
            log(f"⚠️ Core reserve state load failed at {path}: {e}")
            continue
    _state = _default_state()
    return _state


def _save_state():
    """Persist core reserve state to volume."""
    global _state
    if _state is None:
        return False
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path, "w") as f:
                json.dump(_state, f, default=str, indent=2)
            return True
        except Exception:
            continue
    return False


def _record_event(event_type: str, message: str, **extra):
    """Append an audit event to state (capped at last 200)."""
    s = _load_state()
    evt = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "type":    event_type,
        "message": message,
        **extra,
    }
    s["events"].append(evt)
    if len(s["events"]) > 200:
        s["events"] = s["events"][-200:]
    log(f"🏦 CORE: {message}")
    _save_state()


# ──────────────────────────────────────────────────────────────
# PRICE LOOKUPS (defensive — return None on failure, never raise)
# ──────────────────────────────────────────────────────────────
def _get_btc_price() -> float:
    """Current BTC/USDT price from Binance.US."""
    if not binance_get:
        return 0.0
    try:
        r = binance_get("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        if r and "price" in r:
            return float(r["price"])
    except Exception as e:
        log(f"⚠️ Core reserve: BTC price fetch failed: {e}")
    return 0.0


def _get_spy_price() -> float:
    """Current SPY price from Alpaca."""
    if not alpaca:
        return 0.0
    try:
        # Alpaca latest trade endpoint
        r = alpaca("GET", f"/v2/stocks/{EQUITY_TICKER}/trades/latest")
        if r and isinstance(r, dict):
            trade = r.get("trade", {})
            if trade.get("p"):
                return float(trade["p"])
    except Exception as e:
        log(f"⚠️ Core reserve: SPY price fetch failed: {e}")
    return 0.0


def _compute_rsi(prices, period=14):
    """Simple RSI calculation from a list of [ts, price] entries."""
    if len(prices) < period + 1:
        return 50.0   # Insufficient data → neutral
    closes = [p[1] for p in prices[-(period + 1):]]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


# ──────────────────────────────────────────────────────────────
# WALLET / ALLOCATION HELPERS
# ──────────────────────────────────────────────────────────────
def _get_combined_wallet_value() -> float:
    """Stock equity + crypto wallet value. Returns 0 on any failure."""
    total = 0.0
    try:
        if alpaca:
            acct = alpaca("GET", "/v2/account") or {}
            total += float(acct.get("equity", 0) or 0)
    except Exception:
        pass
    try:
        if get_full_wallet:
            wallet = get_full_wallet() or {}
            total += float(wallet.get("total_value", 0) or 0)
    except Exception:
        pass
    return total


def _hours_since(iso_ts: str) -> float:
    """Hours elapsed since an ISO timestamp. Returns infinity if None."""
    if not iso_ts:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return float("inf")


# ──────────────────────────────────────────────────────────────
# ACTIVATION & DEPOSIT LOGIC
# ──────────────────────────────────────────────────────────────
def get_active_reserve_pct(combined_wallet: float) -> float:
    """
    Returns the wallet-scaling reserve % per the user-defined rule:
      < $1k  → 0% (free, no reserve)
      $1k    → 10%
      +1% per $1k
      Cap    → 30% at $21k+
    """
    if combined_wallet < ACTIVATION_THRESHOLD:
        return 0.0
    thousands = int(combined_wallet // 1000)
    pct = 0.10 + (thousands - 1) * 0.01
    return max(0.0, min(0.30, pct))


def get_target_reserve_value(combined_wallet: float) -> float:
    """How much SHOULD be in the reserve given the current wallet."""
    return combined_wallet * get_active_reserve_pct(combined_wallet)


def get_current_reserve_value() -> dict:
    """
    Compute current reserve value from live prices + tracked qty.
    Returns dict with per-slice values and total.
    """
    s = _load_state()
    btc_price = _get_btc_price() or s["btc"].get("entry_price", 0)
    spy_price = _get_spy_price() or s["spy"].get("entry_price", 0)
    btc_value  = s["btc"]["qty"] * btc_price
    spy_value  = s["spy"]["qty"] * spy_price
    cash_value = s["cash_usdt"]
    total      = btc_value + spy_value + cash_value
    return {
        "total":      round(total, 2),
        "btc_value":  round(btc_value, 2),
        "btc_qty":    s["btc"]["qty"],
        "btc_price":  btc_price,
        "spy_value":  round(spy_value, 2),
        "spy_qty":    s["spy"]["qty"],
        "spy_price":  spy_price,
        "cash_value": round(cash_value, 2),
        "btc_pct":    round(btc_value / max(total, 0.01) * 100, 1) if total > 0 else 0,
        "spy_pct":    round(spy_value / max(total, 0.01) * 100, 1) if total > 0 else 0,
        "cash_pct":   round(cash_value / max(total, 0.01) * 100, 1) if total > 0 else 0,
    }


def get_status() -> dict:
    """Full status snapshot for /core_reserve API endpoint."""
    s = _load_state()
    combined = _get_combined_wallet_value()
    target_pct = get_active_reserve_pct(combined)
    target_value = combined * target_pct
    current = get_current_reserve_value()
    # Recent events (last 10)
    recent_events = list(reversed(s.get("events", [])))[:10]
    # Compute total P&L (unrealized + realized via cash deposits)
    total_invested = s["btc"]["total_invested"] + s["spy"]["total_invested"]
    pnl_usd = current["total"] - s["total_contributions"]
    pnl_pct = (pnl_usd / max(s["total_contributions"], 1) * 100) if s["total_contributions"] > 0 else 0
    return {
        "enabled":          ENABLE_CORE_RESERVE,
        "activated":        s["activated"],
        "activated_at":     s.get("activated_at"),
        "activation_threshold": ACTIVATION_THRESHOLD,
        "combined_wallet":  round(combined, 2),
        "target_reserve_pct": round(target_pct * 100, 1),
        "target_reserve_value": round(target_value, 2),
        "actual_reserve_value": current["total"],
        "shortfall":        round(max(0, target_value - current["total"]), 2),
        "slices":           current,
        "target_allocation": {k: round(v * 100, 1) for k, v in TARGET_ALLOCATION.items()},
        "total_contributions": round(s["total_contributions"], 2),
        "pnl_usd":          round(pnl_usd, 2),
        "pnl_pct":          round(pnl_pct, 2),
        "btc_ath":          s["btc"].get("ath_price", 0),
        "spy_ath":          s["spy"].get("ath_price", 0),
        "btc_entry":        s["btc"].get("entry_price", 0),
        "spy_entry":        s["spy"].get("entry_price", 0),
        "last_check":       s.get("last_check"),
        "last_rebalance":   s.get("last_rebalance"),
        "recent_events":    recent_events,
        "state_file":       STATE_FILE,
    }


# ──────────────────────────────────────────────────────────────
# TRADE EXECUTION (delegates to existing order functions)
# ──────────────────────────────────────────────────────────────
def _buy_btc(usd_amount: float) -> bool:
    """Buy BTC with the given USD amount on Binance.US. Tags as core_reserve."""
    if not binance_post or usd_amount < 10:
        return False
    try:
        r = binance_post("/api/v3/order", {
            "symbol":       "BTCUSDT",
            "side":         "BUY",
            "type":         "MARKET",
            "quoteOrderQty": round(usd_amount, 2),
        })
        if r and r.get("orderId"):
            qty   = float(r.get("executedQty", 0))
            paid  = float(r.get("cummulativeQuoteQty", usd_amount))
            price = paid / qty if qty > 0 else 0
            # Update state
            s = _load_state()
            old_qty   = s["btc"]["qty"]
            old_entry = s["btc"]["entry_price"]
            new_qty   = old_qty + qty
            # Volume-weighted avg cost basis
            new_entry = ((old_entry * old_qty) + (price * qty)) / new_qty if new_qty > 0 else price
            s["btc"]["qty"]            = new_qty
            s["btc"]["entry_price"]    = round(new_entry, 2)
            s["btc"]["total_invested"] = round(s["btc"]["total_invested"] + paid, 2)
            s["cash_usdt"]             = max(0.0, s["cash_usdt"] - paid)
            if record_trade:
                try:
                    record_trade(
                        action="buy", symbol="BTCUSDT", qty=qty, price=price,
                        notional=paid, owner="core_reserve",
                        reason="core_reserve_buy",
                    )
                except Exception:
                    pass
            _record_event("BTC_BUY", f"Bought {qty:.6f} BTC @ ${price:,.2f} = ${paid:.2f}",
                          qty=qty, price=price, usd=paid)
            return True
    except Exception as e:
        log(f"⚠️ Core reserve BTC buy failed: {e}")
    return False


def _sell_btc(qty: float) -> bool:
    """Sell BTC from the reserve. Returns USD received to cash slice."""
    if not binance_post or qty <= 0:
        return False
    try:
        # Use minimum-precision quantity
        r = binance_post("/api/v3/order", {
            "symbol":   "BTCUSDT",
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": round(qty, 6),
        })
        if r and r.get("orderId"):
            executed = float(r.get("executedQty", qty))
            received = float(r.get("cummulativeQuoteQty", 0))
            price    = received / executed if executed > 0 else 0
            # Update state
            s = _load_state()
            s["btc"]["qty"]   = max(0.0, s["btc"]["qty"] - executed)
            s["cash_usdt"]    = round(s["cash_usdt"] + received, 2)
            if record_trade:
                try:
                    record_trade(
                        action="sell", symbol="BTCUSDT", qty=executed, price=price,
                        notional=received, owner="core_reserve",
                        reason="core_reserve_sell",
                        pnl_usd=(price - s["btc"]["entry_price"]) * executed,
                    )
                except Exception:
                    pass
            _record_event("BTC_SELL", f"Sold {executed:.6f} BTC @ ${price:,.2f} = ${received:.2f}",
                          qty=executed, price=price, usd=received)
            return True
    except Exception as e:
        log(f"⚠️ Core reserve BTC sell failed: {e}")
    return False


def _buy_spy(usd_amount: float) -> bool:
    """Buy SPY on Alpaca with notional dollars (fractional allowed)."""
    if not alpaca or usd_amount < 1:
        return False
    try:
        r = alpaca("POST", "/v2/orders", body={
            "symbol":         EQUITY_TICKER,
            "notional":       round(usd_amount, 2),
            "side":           "buy",
            "type":           "market",
            "time_in_force":  "day",
            "client_order_id": f"core_buy_spy_{int(datetime.now().timestamp())}",
        })
        if r and r.get("id"):
            # Alpaca fills async; we trust qty and avg_price from order status later
            # For now, estimate from current price
            est_price = _get_spy_price()
            est_qty   = usd_amount / est_price if est_price > 0 else 0
            s = _load_state()
            old_qty   = s["spy"]["qty"]
            old_entry = s["spy"]["entry_price"]
            new_qty   = old_qty + est_qty
            new_entry = ((old_entry * old_qty) + (est_price * est_qty)) / new_qty if new_qty > 0 else est_price
            s["spy"]["qty"]            = round(new_qty, 6)
            s["spy"]["entry_price"]    = round(new_entry, 2)
            s["spy"]["total_invested"] = round(s["spy"]["total_invested"] + usd_amount, 2)
            if record_trade:
                try:
                    record_trade(
                        action="buy", symbol=EQUITY_TICKER, qty=est_qty, price=est_price,
                        notional=usd_amount, owner="core_reserve",
                        reason="core_reserve_buy",
                    )
                except Exception:
                    pass
            _record_event("SPY_BUY", f"Bought ~{est_qty:.4f} {EQUITY_TICKER} @ ~${est_price:.2f} = ${usd_amount:.2f}",
                          qty=est_qty, price=est_price, usd=usd_amount)
            return True
    except Exception as e:
        log(f"⚠️ Core reserve SPY buy failed: {e}")
    return False


def _sell_spy(qty: float) -> bool:
    """Sell SPY shares on Alpaca. Returns USD to cash slice."""
    if not alpaca or qty <= 0:
        return False
    try:
        r = alpaca("POST", "/v2/orders", body={
            "symbol":         EQUITY_TICKER,
            "qty":            round(qty, 6),
            "side":           "sell",
            "type":           "market",
            "time_in_force":  "day",
            "client_order_id": f"core_sell_spy_{int(datetime.now().timestamp())}",
        })
        if r and r.get("id"):
            est_price = _get_spy_price()
            received  = qty * est_price
            s = _load_state()
            s["spy"]["qty"] = max(0.0, s["spy"]["qty"] - qty)
            # SPY proceeds go to Alpaca cash (not USDT) — track separately
            # For simplicity, we credit to cash_usdt and the actual USD sits
            # in Alpaca buying power until manually swept (rare).
            s["cash_usdt"] = round(s["cash_usdt"] + received, 2)
            if record_trade:
                try:
                    record_trade(
                        action="sell", symbol=EQUITY_TICKER, qty=qty, price=est_price,
                        notional=received, owner="core_reserve",
                        reason="core_reserve_sell",
                        pnl_usd=(est_price - s["spy"]["entry_price"]) * qty,
                    )
                except Exception:
                    pass
            _record_event("SPY_SELL", f"Sold {qty:.4f} {EQUITY_TICKER} @ ~${est_price:.2f} = ${received:.2f}",
                          qty=qty, price=est_price, usd=received)
            return True
    except Exception as e:
        log(f"⚠️ Core reserve SPY sell failed: {e}")
    return False


# ──────────────────────────────────────────────────────────────
# DEPOSIT LOGIC (when wallet crosses tier thresholds)
# ──────────────────────────────────────────────────────────────
def check_and_deposit():
    """
    Compare current reserve vs target. If shortfall > $50, deposit
    enough to reach target — split per TARGET_ALLOCATION.
    Called from the boot sequence and once per cycle.
    """
    if not ENABLE_CORE_RESERVE:
        return False
    s = _load_state()
    combined = _get_combined_wallet_value()
    target_value = combined * get_active_reserve_pct(combined)
    current_value = get_current_reserve_value()["total"]

    # Activation guard
    if combined < ACTIVATION_THRESHOLD:
        return False

    # First-time activation
    if not s["activated"] and target_value > 0:
        s["activated"] = True
        s["activated_at"] = datetime.now(timezone.utc).isoformat()
        _record_event("ACTIVATION",
                     f"Core Reserve activated: combined wallet ${combined:.2f} "
                     f"crossed ${ACTIVATION_THRESHOLD:.0f} threshold")

    shortfall = target_value - current_value
    if shortfall < 50:   # Don't make trivial top-ups
        return False

    # Distribute shortfall by target allocation
    btc_buy  = shortfall * TARGET_ALLOCATION["BTC"]
    spy_buy  = shortfall * TARGET_ALLOCATION["SPY"]
    cash_add = shortfall * TARGET_ALLOCATION["USDT"]

    s["total_contributions"] = round(s["total_contributions"] + shortfall, 2)
    _record_event("DEPOSIT",
                 f"Tier deposit: ${shortfall:.2f} (BTC ${btc_buy:.2f} / "
                 f"SPY ${spy_buy:.2f} / Cash ${cash_add:.2f})",
                 total=shortfall)

    success = True
    if btc_buy >= 10:
        success &= _buy_btc(btc_buy)
    if spy_buy >= 1:
        success &= _buy_spy(spy_buy)
    if cash_add > 0:
        s["cash_usdt"] = round(s["cash_usdt"] + cash_add, 2)
        _save_state()
    return success


# ──────────────────────────────────────────────────────────────
# CONTINGENCY MONITOR
# ──────────────────────────────────────────────────────────────
def _update_price_history():
    """Append current prices to rolling 7-day history. Update ATH."""
    s = _load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    btc = _get_btc_price()
    spy = _get_spy_price()
    if btc > 0:
        s["btc_price_history"].append([now_iso, btc])
        if len(s["btc_price_history"]) > PRICE_HISTORY_LEN:
            s["btc_price_history"] = s["btc_price_history"][-PRICE_HISTORY_LEN:]
        if btc > s["btc"].get("ath_price", 0):
            s["btc"]["ath_price"] = btc
    if spy > 0:
        s["spy_price_history"].append([now_iso, spy])
        if len(s["spy_price_history"]) > PRICE_HISTORY_LEN:
            s["spy_price_history"] = s["spy_price_history"][-PRICE_HISTORY_LEN:]
        if spy > s["spy"].get("ath_price", 0):
            s["spy"]["ath_price"] = spy
    s["last_check"] = now_iso
    _save_state()


def _check_defensive_trim():
    """Trigger 1: Defensive trim on >X% drawdown over 7d."""
    s = _load_state()
    fired = []
    # BTC
    if s["btc"]["qty"] > 0 and _hours_since(s["btc"]["last_defensive"]) >= COOLDOWN_DEFENSIVE:
        history = s["btc_price_history"]
        if len(history) >= 24:
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            week_prices = [p[1] for p in history
                           if datetime.fromisoformat(p[0].replace("Z", "+00:00")) >= week_ago]
            if week_prices:
                week_high = max(week_prices)
                current  = _get_btc_price()
                if current > 0 and (week_high - current) / week_high >= DEFENSIVE_TRIM_BTC_DD:
                    sell_qty = s["btc"]["qty"] * DEFENSIVE_TRIM_PCT
                    pct_drop = (week_high - current) / week_high * 100
                    _record_event("DEFENSIVE_TRIM_BTC",
                                 f"BTC -{pct_drop:.1f}% in 7d (${week_high:,.0f} → ${current:,.0f}) "
                                 f"→ trim {DEFENSIVE_TRIM_PCT*100:.0f}% ({sell_qty:.6f} BTC)",
                                 drawdown_pct=pct_drop, sell_qty=sell_qty)
                    if _sell_btc(sell_qty):
                        s["btc"]["last_defensive"] = datetime.now(timezone.utc).isoformat()
                        fired.append("btc_defensive")
    # SPY
    if s["spy"]["qty"] > 0 and _hours_since(s["spy"]["last_defensive"]) >= COOLDOWN_DEFENSIVE:
        history = s["spy_price_history"]
        if len(history) >= 24:
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            week_prices = [p[1] for p in history
                           if datetime.fromisoformat(p[0].replace("Z", "+00:00")) >= week_ago]
            if week_prices:
                week_high = max(week_prices)
                current  = _get_spy_price()
                if current > 0 and (week_high - current) / week_high >= DEFENSIVE_TRIM_SPY_DD:
                    sell_qty = s["spy"]["qty"] * DEFENSIVE_TRIM_PCT
                    pct_drop = (week_high - current) / week_high * 100
                    _record_event("DEFENSIVE_TRIM_SPY",
                                 f"{EQUITY_TICKER} -{pct_drop:.1f}% in 7d "
                                 f"(${week_high:.2f} → ${current:.2f}) "
                                 f"→ trim {DEFENSIVE_TRIM_PCT*100:.0f}% ({sell_qty:.4f} sh)",
                                 drawdown_pct=pct_drop, sell_qty=sell_qty)
                    if _sell_spy(sell_qty):
                        s["spy"]["last_defensive"] = datetime.now(timezone.utc).isoformat()
                        fired.append("spy_defensive")
    if fired:
        _save_state()
    return fired


def _check_opportunity_buy():
    """Trigger 2: Big drop from ATH + RSI oversold = back up the truck."""
    s = _load_state()
    fired = []
    cash_avail = s["cash_usdt"]
    if cash_avail < 10:
        return fired
    # BTC
    if s["btc"].get("ath_price", 0) > 0 and _hours_since(s["btc"]["last_opportunity"]) >= COOLDOWN_OPPORTUNITY:
        current = _get_btc_price()
        ath     = s["btc"]["ath_price"]
        if current > 0:
            dd = (ath - current) / ath
            rsi = _compute_rsi(s["btc_price_history"])
            if dd >= OPPORTUNITY_BUY_BTC_DD and rsi <= OPPORTUNITY_BUY_RSI_MAX:
                spend = cash_avail * OPPORTUNITY_BUY_PCT_OF_CASH
                _record_event("OPPORTUNITY_BUY_BTC",
                             f"BTC -{dd*100:.1f}% from ATH ${ath:,.0f} + RSI {rsi:.0f} oversold "
                             f"→ deploying ${spend:.2f} from cash slice",
                             drawdown_from_ath=dd*100, rsi=rsi, spend=spend)
                if _buy_btc(spend):
                    s["btc"]["last_opportunity"] = datetime.now(timezone.utc).isoformat()
                    fired.append("btc_opportunity")
                    cash_avail -= spend
    # SPY
    if (s["spy"].get("ath_price", 0) > 0
            and _hours_since(s["spy"]["last_opportunity"]) >= COOLDOWN_OPPORTUNITY
            and cash_avail >= 1):
        current = _get_spy_price()
        ath     = s["spy"]["ath_price"]
        if current > 0:
            dd  = (ath - current) / ath
            rsi = _compute_rsi(s["spy_price_history"])
            if dd >= OPPORTUNITY_BUY_SPY_DD and rsi <= OPPORTUNITY_BUY_RSI_MAX:
                spend = cash_avail * OPPORTUNITY_BUY_PCT_OF_CASH
                _record_event("OPPORTUNITY_BUY_SPY",
                             f"{EQUITY_TICKER} -{dd*100:.1f}% from ATH ${ath:.2f} + RSI {rsi:.0f} "
                             f"→ deploying ${spend:.2f}",
                             drawdown_from_ath=dd*100, rsi=rsi, spend=spend)
                if _buy_spy(spend):
                    s["spy"]["last_opportunity"] = datetime.now(timezone.utc).isoformat()
                    fired.append("spy_opportunity")
    if fired:
        _save_state()
    return fired


def _check_take_profit():
    """Trigger 3: Lock partial gains on extreme runs."""
    s = _load_state()
    fired = []
    # BTC
    if s["btc"]["qty"] > 0 and _hours_since(s["btc"]["last_takeprofit"]) >= COOLDOWN_TAKEPROFIT:
        entry  = s["btc"].get("entry_price", 0)
        current = _get_btc_price()
        if entry > 0 and current > 0:
            gain = (current - entry) / entry
            if gain >= TAKE_PROFIT_BTC_GAIN:
                trim_qty = s["btc"]["qty"] * TAKE_PROFIT_TRIM_PCT
                _record_event("TAKE_PROFIT_BTC",
                             f"BTC +{gain*100:.1f}% from entry ${entry:,.0f} → trim "
                             f"{TAKE_PROFIT_TRIM_PCT*100:.0f}% ({trim_qty:.6f} BTC) to lock gain",
                             gain_pct=gain*100, trim_qty=trim_qty)
                if _sell_btc(trim_qty):
                    s["btc"]["last_takeprofit"] = datetime.now(timezone.utc).isoformat()
                    fired.append("btc_takeprofit")
    # SPY
    if s["spy"]["qty"] > 0 and _hours_since(s["spy"]["last_takeprofit"]) >= COOLDOWN_TAKEPROFIT:
        entry   = s["spy"].get("entry_price", 0)
        current = _get_spy_price()
        if entry > 0 and current > 0:
            gain = (current - entry) / entry
            if gain >= TAKE_PROFIT_SPY_GAIN:
                trim_qty = s["spy"]["qty"] * TAKE_PROFIT_TRIM_PCT
                _record_event("TAKE_PROFIT_SPY",
                             f"{EQUITY_TICKER} +{gain*100:.1f}% from entry ${entry:.2f} "
                             f"→ trim {TAKE_PROFIT_TRIM_PCT*100:.0f}% ({trim_qty:.4f} sh)",
                             gain_pct=gain*100, trim_qty=trim_qty)
                if _sell_spy(trim_qty):
                    s["spy"]["last_takeprofit"] = datetime.now(timezone.utc).isoformat()
                    fired.append("spy_takeprofit")
    if fired:
        _save_state()
    return fired


def _check_drift_rebalance():
    """Trigger 4: Bring slice weights back to target if any drifted >15%."""
    s = _load_state()
    if _hours_since(s.get("last_rebalance")) < COOLDOWN_REBALANCE:
        return []
    current = get_current_reserve_value()
    total = current["total"]
    if total < 100:    # Too small to bother
        return []
    actual = {
        "BTC":  current["btc_value"]  / total,
        "SPY":  current["spy_value"]  / total,
        "USDT": current["cash_value"] / total,
    }
    max_drift = max(abs(actual[k] - TARGET_ALLOCATION[k]) for k in TARGET_ALLOCATION)
    if max_drift < REBALANCE_DRIFT_THRESHOLD:
        return []
    _record_event("REBALANCE",
                 f"Drift {max_drift*100:.1f}% > {REBALANCE_DRIFT_THRESHOLD*100:.0f}% — rebalancing "
                 f"(BTC {actual['BTC']*100:.0f}% → 50%, "
                 f"SPY {actual['SPY']*100:.0f}% → 30%, "
                 f"Cash {actual['USDT']*100:.0f}% → 20%)",
                 drift_pct=max_drift*100)
    # Trim overweight slices first, then top up underweight
    for slice_name in ["BTC", "SPY"]:
        target_value = total * TARGET_ALLOCATION[slice_name]
        slice_key = "btc" if slice_name == "BTC" else "spy"
        actual_value = current[f"{slice_key}_value"]
        if actual_value > target_value * 1.05:    # Trim overweight
            excess = actual_value - target_value
            current_price = current[f"{slice_key}_price"]
            if current_price > 0:
                qty_to_sell = excess / current_price
                if slice_name == "BTC":
                    _sell_btc(qty_to_sell)
                else:
                    _sell_spy(qty_to_sell)
    # Re-read state — cash slice now has the trim proceeds
    s = _load_state()
    cash = s["cash_usdt"]
    target_cash = total * TARGET_ALLOCATION["USDT"]
    excess_cash = cash - target_cash
    if excess_cash > 50:
        current = get_current_reserve_value()    # Refresh prices
        # Distribute excess between BTC and SPY proportionally
        new_total   = current["total"] + excess_cash    # virtual after redeploy
        btc_target  = new_total * TARGET_ALLOCATION["BTC"]
        spy_target  = new_total * TARGET_ALLOCATION["SPY"]
        btc_short   = max(0, btc_target - current["btc_value"])
        spy_short   = max(0, spy_target - current["spy_value"])
        total_short = btc_short + spy_short
        if total_short > 0:
            btc_buy = excess_cash * (btc_short / total_short)
            spy_buy = excess_cash * (spy_short / total_short)
            if btc_buy >= 10:
                _buy_btc(btc_buy)
            if spy_buy >= 1:
                _buy_spy(spy_buy)
    s = _load_state()
    s["last_rebalance"] = datetime.now(timezone.utc).isoformat()
    _save_state()
    return ["rebalance"]


# ──────────────────────────────────────────────────────────────
# MAIN MONITOR LOOP (called once per hour by bot's main loop)
# ──────────────────────────────────────────────────────────────
def run_hourly_check():
    """
    Master watcher — called once per hour from bot's main loop.
    Runs price update, deposit check, then all 4 contingency checks.
    Returns dict summary of what fired.
    """
    if not ENABLE_CORE_RESERVE:
        return {"enabled": False}
    fired_all = []
    try:
        _update_price_history()
        # Try deposit if shortfall exists
        if check_and_deposit():
            fired_all.append("deposit")
        # Only check contingencies if we have positions to watch
        s = _load_state()
        if s["activated"]:
            fired_all.extend(_check_defensive_trim())
            fired_all.extend(_check_opportunity_buy())
            fired_all.extend(_check_take_profit())
            fired_all.extend(_check_drift_rebalance())
    except Exception as e:
        log(f"⚠️ Core reserve hourly check failed: {e}")
    return {
        "enabled":   True,
        "activated": _load_state()["activated"],
        "fired":     fired_all,
    }
