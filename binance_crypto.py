"""
binance_crypto.py
══════════════════════════════════════════════════════════════════
Binance.US Crypto Trading Module for NovaTrade
Drop in same folder as bot_with_proxy.py

FEATURES:
  - 24/7 crypto trading via Binance.US API
  - Near-zero fees: 0% maker, 0.01% taker (vs 0.25% on Alpaca)
  - 8-coin universe: BTC, ETH, SOL, AVAX, DOGE, LINK, ADA, DOT
  - Separate cash pool — never competes with stock funds
  - 72-hour hard exit — fast 2-3 day turnaround
  - Projection engine adapted for crypto volatility
  - Minimum 2.5% profit target to cover fees + meaningful return
  - Always limit orders (maker = 0% fee)
  - Full Claude + Grok AI collaboration on crypto decisions
  - Runs 24/7 including weekends and after hours

ENVIRONMENT VARIABLES REQUIRED (set in Railway):
  BINANCE_KEY    — your Binance.US API key
  BINANCE_SECRET — your Binance.US API secret

INTEGRATION (3 lines in bot_with_proxy.py):
  from binance_crypto import CryptoTrader
  crypto_trader = CryptoTrader()          # module-level init
  # In trading_loop: crypto_trader.run_crypto_cycle(equity)
"""

import os
import time
import math
import hmac
import hashlib
import requests
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

BINANCE_BASE    = "https://api.binance.us"
BINANCE_KEY     = os.environ.get("BINANCE_KEY", "")
BINANCE_SECRET  = os.environ.get("BINANCE_SECRET", "")

# ── Crypto Universe ───────────────────────────────────────────
# 8 high-liquidity coins available on Binance.US
# Symbol format: BTCUSDT (Binance.US format)
CRYPTO_UNIVERSE = {
    # ── Tier 1: High volume majors ────────────────────────────
    "BTCUSDT":   {"name": "Bitcoin",      "min_notional": 10.0, "decimals": 5},
    "ETHUSDT":   {"name": "Ethereum",     "min_notional": 10.0, "decimals": 4},
    "XRPUSDT":   {"name": "XRP",          "min_notional": 10.0, "decimals": 0},
    "SOLUSDT":   {"name": "Solana",       "min_notional": 10.0, "decimals": 3},
    "ADAUSDT":   {"name": "Cardano",      "min_notional": 10.0, "decimals": 1},
    "DOGEUSDT":  {"name": "Dogecoin",     "min_notional": 10.0, "decimals": 0},
    "AVAXUSDT":  {"name": "Avalanche",    "min_notional": 10.0, "decimals": 3},
    "LINKUSDT":  {"name": "Chainlink",    "min_notional": 10.0, "decimals": 3},
    "DOTUSDT":   {"name": "Polkadot",     "min_notional": 10.0, "decimals": 3},
    "LTCUSDT":   {"name": "Litecoin",     "min_notional": 10.0, "decimals": 3},
    # ── Tier 2: Layer 1/2 ecosystems ─────────────────────────
    "MATICUSDT": {"name": "Polygon",      "min_notional": 10.0, "decimals": 0},
    "ATOMUSDT":  {"name": "Cosmos",       "min_notional": 10.0, "decimals": 2},
    "NEARUSDT":  {"name": "NEAR Protocol","min_notional": 10.0, "decimals": 1},
    "ALGOUSDT":  {"name": "Algorand",     "min_notional": 10.0, "decimals": 0},
    "UNIUSDT":   {"name": "Uniswap",      "min_notional": 10.0, "decimals": 2},
    # ── Tier 3: Meme coins (high volatility = high opportunity) ─
    "SHIBUSDT":  {"name": "Shiba Inu",    "min_notional": 10.0, "decimals": 0},
    "PEPEUSDT":  {"name": "Pepe",         "min_notional": 10.0, "decimals": 0},
    # ── Your current holdings — always tracked ────────────────
    "FETUSDT":   {"name": "Fetch.ai",     "min_notional": 10.0, "decimals": 1},
    "AUDIOUSDT": {"name": "Audius",       "min_notional": 10.0, "decimals": 1},
    "KAVAUSDT":  {"name": "Kava",         "min_notional": 10.0, "decimals": 3},
    "RVNUSDT":   {"name": "Ravencoin",    "min_notional": 10.0, "decimals": 0},
}

# ── Crypto Trading Rules ──────────────────────────────────────
# ── Display / Tradability Thresholds ──────────────────────────
# Holdings below MIN_DISPLAY_VALUE are treated as dust:
#   • hidden from cycle logs and snapshot wallet output
#   • stripped from AI prompts (saves tokens, removes noise)
#   • still tracked internally so we never "forget" a coin
# MIN_TRADABLE_VALUE is Binance.US min_notional ($10 effective)
MIN_DISPLAY_VALUE  = 1.00     # Hide anything below $1 in logs/AI prompts
MIN_TRADABLE_VALUE = 10.00    # Binance.US won't accept sells below this

# ── AI Competition (Claude vs Grok independent trading) ──────
# Each AI gets its own slice of the USDT pool. They cannot starve each
# other and the same coin can be held independently by both AIs at once.
# Set ENABLE_AI_COMPETITION=False to revert to the old shared-pool merge.
ENABLE_AI_COMPETITION = True
CLAUDE_POOL_PCT       = 0.50   # Claude's share of free USDT
GROK_POOL_PCT         = 0.50   # Grok's share of free USDT
# Note: Pool % must sum to 1.0. Reserved/safety USDT is taken from both.

# ── Wallet-Scaling Reserve Rule ──────────────────────────────
# Below $1000 combined wallet → 0% reserve (AIs trade with full balance).
# At $1000 → 10% reserve, +1% per additional $1000, capped at 30% at $21k+.
# This protects accumulated profit while letting small accounts compound
# without artificial restraint.
RESERVE_FREE_THRESHOLD = 1000.0    # Below this → no forced reserve
RESERVE_BASE_PCT       = 0.10      # Reserve % at $1000
RESERVE_STEP_PCT       = 0.01      # Added per $1000 above $1000
RESERVE_CAP_PCT        = 0.30      # Maximum reserve at $21k+

def get_wallet_reserve_pct(combined_wallet_value: float) -> float:
    """
    Wallet-scaling reserve rule (user-defined Phase 1):

      <  $1,000  → 0%   (AIs have full freedom)
      $1,000–$1,999 → 10%
      $2,000–$2,999 → 11%
      ...
      $21,000+      → 30% (cap)

    Returns reserve_pct as float (0.0 to 0.30).
    """
    try:
        w = float(combined_wallet_value or 0)
    except (TypeError, ValueError):
        return 0.0
    if w < RESERVE_FREE_THRESHOLD:
        return 0.0
    # Each completed $1000 above $0 adds 1% starting at 10% for the first $1000
    thousands = int(w // 1000)              # 1, 2, 3, ...
    pct = RESERVE_BASE_PCT + (thousands - 1) * RESERVE_STEP_PCT
    return max(0.0, min(RESERVE_CAP_PCT, pct))

def get_wallet_reserve_label(combined_wallet_value: float) -> str:
    """Human-readable description of the active reserve tier."""
    w = float(combined_wallet_value or 0)
    pct = get_wallet_reserve_pct(w)
    if pct == 0.0:
        return f"FREE (under ${RESERVE_FREE_THRESHOLD:.0f}) — AIs trade full balance"
    if pct >= RESERVE_CAP_PCT:
        return f"{pct*100:.0f}% (cap at $21k+)"
    return f"{pct*100:.0f}% (scaling: +1% per $1k)"

CRYPTO_RULES = {
    # ── Stop / Profit targets (AGGRESSIVE SCALPING) ──────────
    # Strategy: take profits fast, small positions, compound many wins
    # Previous (too patient): 20% stop / 50% TP / 30% trail / 72h hold
    "stop_loss_pct":        0.08,    # -8% stop (was -20% — cuts losses fast)
    "take_profit_pct":      0.08,    # +8% TP (was +50% — banks wins fast)
    "trail_activate_pct":   0.03,    # Start trailing at +3% (was +30%)
    "trail_pct":            0.025,   # Tight 2.5% trail (was 40% — lock gains fast)
    "min_profit_pct":       0.03,    # 3% minimum expected (was 5%)
    "max_hold_hours":       24,      # 1-day soft time stop (was 72h)
    # If position is underwater AND below fee floor at the soft time stop,
    # extend up to this many hours total before forcing exit. This prevents
    # the bot from dumping ETH at -1.38% just because 24h elapsed when a
    # small wait could break even or turn green.
    "hard_max_hold_hours":  72,      # Absolute ceiling — exit no matter what
    # ── Position sizing (tier-based) ─────────────────────────
    # More aggressive at small equity — needed to compound to goal
    "max_positions":        3,       # Aggressive — 3 concurrent crypto positions (was 2)
    "min_trade_usdt":       10.0,    # Binance.US minimum notional ($10)
    # Order floor: min notional + buffer so fees/price ticks between
    # sizing and fill never push an order under the exchange minimum.
    "min_order_usdt":       10.50,
    # ── Entry filters ─────────────────────────────────────────
    "min_confidence":       60,      # Aggressive — lowered to 60 (was 65)
    "vol_spike_multiplier": 1.5,     # Volume must be 1.5x average to confirm breakout
    "breakout_periods":     20,      # 20-period high breakout trigger
    "rsi_momentum_min":     55,      # RSI must be above 55 for momentum entry
    "rsi_oversold_max":     35,      # RSI below 35 = oversold dip entry
    # ── Fees (Binance.US — updated April 22, 2026) ────────────
    # New flat fee schedule effective for all users with no tiers:
    #   0% maker · 0.02% taker · all pairs
    # BNB fee discount: hold any BNB balance → automatic 5% off all fees
    #   (paid in BNB). Effective taker with BNB = 0.019%.
    # We model the WORST case (taker both sides, no BNB) for safety, and
    # apply BNB discount only when bnb_balance > 0 is detected at runtime.
    "maker_fee":            0.0000,    # 0% — confirmed (limit order, resting on book)
    "taker_fee":            0.0002,    # 0.02% — confirmed Binance.US 2026 rate
    # Round trip = worst case: market in + market out = 2 × taker = 0.04%.
    # Best case (limit in + limit out) = 0%.
    # Realistic case (limit in + market stop out) = 0.02%.
    # We add a 50% safety margin (so 0.04% × 1.5 = 0.06%) for slippage.
    "round_trip_fee":       0.0006,
    "bnb_fee_discount":     0.05,      # 5% off when holding any BNB balance
    # ── Minimum-profit floor ───────────────────────────────────
    # No trade is taken unless projected gain ≥ this × round_trip_fee.
    # Default 3x → projected gain must exceed 0.18% to even consider entry.
    # This is the "won't take a trade unless math works" guardrail.
    "min_profit_fee_multiple": 3.0,
    # ── Drawdown protection ────────────────────────────────────
    "global_drawdown_pause": 0.40,   # Pause ALL trading if equity drops 40%
    # ── Projection filters ────────────────────────────────────
    "proj_conf_threshold":  55,      # Lower threshold = more opportunities
    "proj_min_range_pct":   0.05,    # 5%+ range for viable setup
    "crypto_pool_pct":      0.30,
}

# ── Tier-based risk sizing ─────────────────────────────────────
# At small equity we must take bigger % risks to compound toward goal
# As equity grows, risk per trade shrinks (protecting gains)
# tp_pct: quick-flip take-profit override (classic-strategy positions
# only — Turtle keeps its own Donchian/2N exit, never a fixed TP). At
# small wallet size we bank any real gain fast and redeploy the cash
# instead of waiting for the flat 8% CRYPTO_RULES["take_profit_pct"].
# None at the top tier = no override, existing 8% tp_price stands.
CRYPTO_TIERS = [
    # AGGRESSIVE PROFILE + DISCOVERY MODE ENABLED
    # coins list is ADVISORY (prefer these) — AI can buy anything from scan
    # if confidence ≥ 70%. No hard tier restrictions on buys.
    {"min_equity":   0, "max_equity": 150,  "risk_pct": 0.30, "max_pos": 3, "tp_pct": 0.015,
     "coins": None,  # DISCOVERY MODE — AI can pick from top market movers
     "note": "Tier 1 — AGGRESSIVE + DISCOVERY: 30% risk, 3 positions, any trending coin, 1.5% quick TP"},
    {"min_equity": 150, "max_equity": 300,  "risk_pct": 0.25, "max_pos": 3, "tp_pct": 0.03,
     "coins": None,
     "note": "Tier 2 — 25% risk, 3 positions, full discovery, 3% quick TP"},
    {"min_equity": 300, "max_equity": 600,  "risk_pct": 0.20, "max_pos": 4, "tp_pct": 0.05,
     "coins": None,
     "note": "Tier 3 — 20% risk, 4 positions, full discovery, 5% quick TP"},
    {"min_equity": 600, "max_equity": 9999, "risk_pct": 0.15, "max_pos": 5, "tp_pct": None,
     "coins": None,
     "note": "Tier 4 — 15% risk, 5 positions, full discovery, standard 8% TP (patient)"},
]

def effective_fees(has_bnb: bool = False) -> dict:
    """
    Returns effective Binance.US fee rates given current BNB holdings.

    With BNB balance > 0, Binance.US automatically pays fees from BNB
    and applies a 5% discount on top of the base rate.

    Returns dict with maker, taker, and round_trip floats (decimal, not %).
    """
    base_maker = CRYPTO_RULES["maker_fee"]      # 0.0000
    base_taker = CRYPTO_RULES["taker_fee"]      # 0.0002
    base_rt    = CRYPTO_RULES["round_trip_fee"] # 0.0006
    if has_bnb:
        disc = 1.0 - CRYPTO_RULES["bnb_fee_discount"]   # 0.95
        return {
            "maker":      round(base_maker * disc, 6),
            "taker":      round(base_taker * disc, 6),
            "round_trip": round(base_rt    * disc, 6),
            "bnb_active": True,
        }
    return {
        "maker":      base_maker,
        "taker":      base_taker,
        "round_trip": base_rt,
        "bnb_active": False,
    }


def get_crypto_tier(wallet_value: float) -> dict:
    """Get current tier based on wallet total value."""
    for t in CRYPTO_TIERS:
        if t["min_equity"] <= wallet_value < t["max_equity"]:
            return t
    return CRYPTO_TIERS[-1]


def get_crypto_quick_take_profit_pct(wallet_value: float):
    """
    Small-wallet quick-flip take-profit override, or None at the top
    tier (meaning: use the existing fixed 8% tp_price / trailing logic
    as-is). Single source of truth for run_exit_monitor.
    """
    return get_crypto_tier(wallet_value).get("tp_pct")


# ══════════════════════════════════════════════════════════════
# BINANCE.US API CLIENT
# ══════════════════════════════════════════════════════════════

def _sign(params: dict) -> str:
    """HMAC-SHA256 signature required for private Binance.US endpoints."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(
        BINANCE_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def _headers() -> dict:
    return {
        "X-MBX-APIKEY": BINANCE_KEY,
        "Content-Type": "application/json",
    }

def _timestamp() -> int:
    return int(time.time() * 1000)

def binance_get(path: str, params: dict = None, signed: bool = False) -> dict:
    """GET request to Binance.US API."""
    params = params or {}
    if signed:
        params["timestamp"] = _timestamp()
        params["signature"] = _sign(params)
    try:
        resp = requests.get(
            f"{BINANCE_BASE}{path}",
            params=params,
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise Exception(f"Binance GET {path} failed: {e}")

def binance_post(path: str, params: dict) -> dict:
    """POST request to Binance.US API (always signed)."""
    params["timestamp"] = _timestamp()
    params["signature"] = _sign(params)
    try:
        resp = requests.post(
            f"{BINANCE_BASE}{path}",
            params=params,
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise Exception(f"Binance POST {path} failed: {e}")

def binance_delete(path: str, params: dict) -> dict:
    """DELETE request to Binance.US API (cancel orders)."""
    params["timestamp"] = _timestamp()
    params["signature"] = _sign(params)
    try:
        resp = requests.delete(
            f"{BINANCE_BASE}{path}",
            params=params,
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise Exception(f"Binance DELETE {path} failed: {e}")


# ══════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════

# Known rebranded/migrated tokens — try alternate symbols if primary fails
_SYMBOL_ALIASES = {
    # FET merged into ASI Alliance — try multiple possible symbols
    "FETUSDT":  ["FETUSDT", "AIUSDT", "ASIUSDT"],
    "OCEANUSDT":["OCEANUSDT", "ASIUSDT"],
    "AGIXUSDT": ["AGIXUSDT", "ASIUSDT"],
}

# Cache: if we successfully bought/sold a symbol, it definitely works
_VERIFIED_SYMBOLS: set = set()

def get_crypto_price(symbol: str) -> float:
    """
    Get current price for a crypto symbol.
    Uses 24hr ticker as fallback. Handles rebranded tokens with aliases.
    Caches verified symbols so we don't retry dead aliases.
    """
    # If we've already verified this symbol works, use it directly
    if symbol in _VERIFIED_SYMBOLS:
        try:
            data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
            price = float(data.get("price", 0))
            if price > 0:
                return price
        except Exception:
            pass

    symbols_to_try = _SYMBOL_ALIASES.get(symbol, [symbol])

    for sym in symbols_to_try:
        try:
            data  = binance_get("/api/v3/ticker/price", {"symbol": sym})
            price = float(data.get("price", 0))
            if price > 0:
                _VERIFIED_SYMBOLS.add(symbol)  # Remember what worked
                if sym != symbol:
                    _VERIFIED_SYMBOLS.add(sym)
                return price
        except Exception:
            pass

        try:
            data  = binance_get("/api/v3/ticker/24hr", {"symbol": sym})
            price = float(data.get("lastPrice", 0))
            if price > 0:
                _VERIFIED_SYMBOLS.add(symbol)
                return price
        except Exception:
            pass

    raise ValueError(f"Cannot get price for {symbol} (tried: {symbols_to_try})")

def get_crypto_bars(symbol: str, interval: str = "1h", limit: int = 168) -> list:
    """
    Fetch OHLCV bars for a crypto symbol.
    Default: 1h bars for 7 days (168 hours) — enough for indicators + projection.
    Returns list of dicts: {t, o, h, l, c, v}
    """
    data = binance_get("/api/v3/klines", {
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    })
    bars = []
    for k in data:
        bars.append({
            "t": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat(),
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "v": float(k[5]),
        })
    return bars

def get_crypto_24h_stats(symbol: str) -> dict:
    """Get 24h price change stats — momentum signal."""
    data = binance_get("/api/v3/ticker/24hr", {"symbol": symbol})
    return {
        "symbol":       symbol,
        "price":        float(data["lastPrice"]),
        "change_pct":   float(data["priceChangePercent"]),
        "high":         float(data["highPrice"]),
        "low":          float(data["lowPrice"]),
        "volume":       float(data["volume"]),
        "quote_volume": float(data["quoteVolume"]),
    }


# ══════════════════════════════════════════════════════════════
# TURTLE TRADING SIGNALS
# ══════════════════════════════════════════════════════════════
# Wraps turtle_math primitives with crypto-specific data fetching.
# Used as the entry gate when a Turtle playbook is active.

def turtle_check_entry(symbol: str, system: int = 1) -> dict:
    """
    Check if a crypto symbol is a valid Turtle entry RIGHT NOW.
    Fetches daily bars and evaluates the Donchian breakout signal.

    Returns dict with eligible, reason, entry_level, stop_price, atr, system.
    """
    try:
        from turtle_math import compute_turtle_signal
    except ImportError as e:
        return {"eligible": False, "reason": f"turtle_math import failed: {e}",
                "entry_level": None, "stop_price": None, "atr": None, "system": system}
    try:
        bars = get_crypto_bars(symbol, interval="1d", limit=90)
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
                "reason": f"{period}d Donchian breakout: ${sig['current_close']:.4f} > ${sig['entry_level']:.4f}",
                "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
                "atr": sig["atr"], "system": system}
    return {"eligible": False,
            "reason": f"no {period}d breakout: ${sig['current_close']:.4f} ≤ ${sig['entry_level']:.4f}",
            "entry_level": sig["entry_level"], "stop_price": sig["stop_price"],
            "atr": sig["atr"], "system": system}


def turtle_check_exit(symbol: str, entry_price: float, atr_at_entry: float,
                     system: int = 1) -> dict:
    """Check if a Turtle crypto position should be closed now."""
    try:
        from turtle_math import should_turtle_exit
    except ImportError as e:
        return {"should_exit": False, "reason": f"turtle_math import failed: {e}", "exit_level": None}
    try:
        bars = get_crypto_bars(symbol, interval="1d", limit=90)
    except Exception as e:
        return {"should_exit": False, "reason": f"bar fetch failed: {e}", "exit_level": None}
    if not bars:
        return {"should_exit": False, "reason": "no bars", "exit_level": None}
    return should_turtle_exit(bars, entry_price, atr_at_entry, system=system)


def get_all_crypto_stats() -> list:
    """Get 24h stats for all coins in our universe — sorted by momentum."""
    stats = []
    for symbol in CRYPTO_UNIVERSE:
        try:
            s = get_crypto_24h_stats(symbol)
            stats.append(s)
        except Exception:
            continue
    return sorted(stats, key=lambda x: abs(x["change_pct"]), reverse=True)


def scan_binance_market(min_volume_usdt: float = 500_000,
                        top_n: int = 10) -> list:
    """
    Scan ALL USDT pairs on Binance.US for top movers.
    Returns top_n coins by momentum that have enough volume to trade.
    This lets the AI discover opportunities OUTSIDE our fixed universe.

    Returns list of dicts with symbol, change_pct, volume, price.
    """
    try:
        # Get all 24h tickers in one call
        all_tickers = binance_get("/api/v3/ticker/24hr", {})
        if not isinstance(all_tickers, list):
            return []

        # Filter: USDT pairs only, min volume, exclude stablecoins
        stables = {"USDT", "BUSD", "DAI", "USDC", "TUSD", "USDP"}
        candidates = []
        for t in all_tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            asset = sym.replace("USDT", "")
            if asset in stables:
                continue
            vol = float(t.get("quoteVolume", 0))
            if vol < min_volume_usdt:
                continue
            change = float(t.get("priceChangePercent", 0))
            candidates.append({
                "symbol":     sym,
                "asset":      asset,
                "price":      float(t.get("lastPrice", 0)),
                "change_pct": change,
                "volume_m":   round(vol / 1_000_000, 1),
                "in_universe": sym in CRYPTO_UNIVERSE,
            })

        # Sort by absolute momentum — biggest movers first
        candidates.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

        # Return top N — mix of universe + new discoveries
        return candidates[:top_n]

    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# CRYPTO TECHNICAL INDICATORS
# Calibrated for crypto volatility (higher ATR multipliers)
# ══════════════════════════════════════════════════════════════

def compute_crypto_indicators(bars: list) -> dict:
    """
    Compute technical indicators from crypto OHLCV bars.
    Uses hourly bars — adapted from stock indicator logic.
    Returns same dict structure as stock compute_indicators().
    """
    if not bars or len(bars) < 26:
        return {}

    closes  = [b["c"] for b in bars]
    highs   = [b["h"] for b in bars]
    lows    = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]

    def sma(data, n):
        return sum(data[-n:]) / n if len(data) >= n else None

    def ema(data, n):
        if len(data) < n:
            return None
        k = 2 / (n + 1)
        e = sum(data[:n]) / n
        for d in data[n:]:
            e = d * k + e * (1 - k)
        return round(e, 4)

    def rsi(data, n=14):
        if len(data) < n + 1:
            return None
        g, l = [], []
        for i in range(1, len(data)):
            d = data[i] - data[i-1]
            g.append(max(d, 0))
            l.append(max(-d, 0))
        if len(g) < n:
            return None
        ag = sum(g[-n:]) / n
        al = sum(l[-n:]) / n
        return round(100 - (100 / (1 + ag / al)), 2) if al else 100

    def atr(highs, lows, closes, n=14):
        """Average True Range — crypto needs this for stop placement."""
        if len(closes) < n + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)
        return round(sum(trs[-n:]) / n, 4) if len(trs) >= n else None

    close    = closes[-1]
    sma20    = sma(closes, 20)
    sma50    = sma(closes, 50)
    ema9     = ema(closes[-20:], 9)
    ema21    = ema(closes[-30:], 21)
    macd_val = round(ema(closes[-30:], 12) - ema(closes, 26), 4) if ema(closes[-30:], 12) and ema(closes, 26) else None
    rsi_val  = rsi(closes)
    atr_val  = atr(highs, lows, closes)

    # Bollinger Bands
    bb_pct = None
    if sma20:
        std = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
        bb_u = sma20 + 2 * std
        bb_l = sma20 - 2 * std
        bb_pct = round((close - bb_l) / (bb_u - bb_l) * 100, 1) if bb_u != bb_l else 50

    # Volume ratio
    avg_vol   = sum(volumes[-24:]) / 24 if len(volumes) >= 24 else None
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else None

    # 24h momentum (using hourly bars)
    mom_24h = round((closes[-1] - closes[-25]) / closes[-25] * 100, 2) if len(closes) >= 25 else None
    # 7d momentum
    mom_7d  = round((closes[-1] - closes[-169]) / closes[-169] * 100, 2) if len(closes) >= 169 else None

    # ── Breakout detection (20-period high/low) ───────────────
    # Grok strategy: enter on 20-period HIGH breakout + volume >1.5x
    periods = CRYPTO_RULES["breakout_periods"]   # 20
    breakout_high = None
    breakout_low  = None
    is_breakout_up   = False
    is_breakout_down = False

    if len(highs) >= periods + 1:
        period_high = max(highs[-periods-1:-1])   # High of last 20 bars (excluding current)
        period_low  = min(lows[-periods-1:-1])    # Low of last 20 bars
        breakout_high = round(period_high, 6)
        breakout_low  = round(period_low, 6)

        # Current close breaks above 20-period high = bullish breakout
        is_breakout_up   = close > period_high
        # Current close breaks below 20-period low = bearish breakdown
        is_breakout_down = close < period_low

    vol_spike = (vol_ratio or 0) >= CRYPTO_RULES["vol_spike_multiplier"]

    # Combined breakout signal: price breakout + volume confirmation
    breakout_signal = "BULLISH_BREAKOUT" if (is_breakout_up and vol_spike) \
                 else "BEARISH_BREAKDOWN" if (is_breakout_down and vol_spike) \
                 else "BREAKOUT_NO_VOL"   if (is_breakout_up or is_breakout_down) \
                 else "NO_BREAKOUT"

    # ── Momentum quality score (0-100) ───────────────────────
    # Higher = better entry opportunity
    momentum_score = 0
    if rsi_val:
        if CRYPTO_RULES["rsi_momentum_min"] <= rsi_val <= 75:
            momentum_score += 30  # Strong momentum, not overbought
        elif rsi_val <= CRYPTO_RULES["rsi_oversold_max"]:
            momentum_score += 25  # Oversold dip
    if vol_spike:
        momentum_score += 30  # Volume confirms
    if is_breakout_up:
        momentum_score += 25  # Price breakout
    if macd_val and macd_val > 0:
        momentum_score += 15  # MACD positive

    return {
        "close":            round(close, 4),
        "rsi":              rsi_val,
        "macd":             macd_val,
        "sma20":            round(sma20, 4) if sma20 else None,
        "sma50":            round(sma50, 4) if sma50 else None,
        "ema9":             round(ema9, 4) if ema9 else None,
        "ema21":            round(ema21, 4) if ema21 else None,
        "bb_pct":           bb_pct,
        "vol_ratio":        vol_ratio,
        "vol_spike":        vol_spike,
        "atr":              atr_val,
        "mom_24h":          mom_24h,
        "mom_7d":           mom_7d,
        "breakout_high":    breakout_high,
        "breakout_low":     breakout_low,
        "is_breakout_up":   is_breakout_up,
        "is_breakout_down": is_breakout_down,
        "breakout_signal":  breakout_signal,
        "momentum_score":   momentum_score,
    }


# ══════════════════════════════════════════════════════════════
# CRYPTO PROJECTION ENGINE
# Adapted from stock projection_engine.py for crypto volatility
# ══════════════════════════════════════════════════════════════

def get_crypto_projection(symbol: str, bars: list, ind: dict) -> dict:
    """Compute price projection for a crypto symbol."""
    if not ind or not bars or len(bars) < 24:
        return {"symbol": symbol, "error": "insufficient data"}

    close = ind["close"]
    atr   = ind.get("atr", close * 0.02)  # Default 2% if missing

    # ── Layer 1: Pivot Point (last 24h H/L/C) ──────────────────
    last_24 = bars[-24:]
    h24 = max(b["h"] for b in last_24)
    l24 = min(b["l"] for b in last_24)
    c24 = bars[-1]["c"]
    pivot   = round((h24 + l24 + c24) / 3, 4)
    r1      = round(2 * pivot - l24, 4)
    s1      = round(2 * pivot - h24, 4)

    # ── Layer 2: ATR-based range ───────────────────────────────
    # Crypto uses 1.5x ATR for range (stocks use 1x)
    atr_high = round(close + atr * 1.5, 4)
    atr_low  = round(close - atr * 1.5, 4)

    # ── Layer 3: Trend context ─────────────────────────────────
    bias = "neutral"
    trend_score = 0
    if ind.get("ema9") and ind.get("ema21"):
        if ind["ema9"] > ind["ema21"]:
            trend_score += 1
            bias = "bullish"
        else:
            trend_score -= 1
            bias = "bearish"
    if ind.get("sma20") and close > ind["sma20"]:
        trend_score += 1
    if ind.get("macd") and ind["macd"] > 0:
        trend_score += 1

    # ── Layer 4: Momentum ──────────────────────────────────────
    momentum_score = 0
    rsi = ind.get("rsi", 50)
    if rsi:
        if rsi < 35:
            momentum_score += 2   # Oversold — bounce likely
        elif rsi > 65:
            momentum_score -= 1   # Overbought — caution
    if ind.get("mom_24h") and ind["mom_24h"] > 3:
        momentum_score += 1
    if ind.get("vol_ratio") and ind["vol_ratio"] > 1.5:
        momentum_score += 1

    # ── Layer 5: Bollinger Band position ──────────────────────
    bb_score = 0
    bb_pct = ind.get("bb_pct", 50)
    if bb_pct is not None:
        if bb_pct < 20:
            bb_score = 2    # Near lower band — potential bounce
        elif bb_pct > 80:
            bb_score = -1   # Near upper band — potential rejection

    # ── Combine layers ─────────────────────────────────────────
    total_score = trend_score + momentum_score + bb_score
    if total_score >= 2:
        bias = "bullish"
    elif total_score <= -2:
        bias = "bearish"

    # Final projected range — blend pivot and ATR
    proj_high = round((r1 * 0.5 + atr_high * 0.5), 4)
    proj_low  = round((s1 * 0.5 + atr_low  * 0.5), 4)

    # Ensure proj_low < current < proj_high makes sense
    if proj_low >= close:
        proj_low = round(close * 0.975, 4)
    if proj_high <= close:
        proj_high = round(close * 1.025, 4)

    # ── Confidence score ───────────────────────────────────────
    range_pct = round((proj_high - proj_low) / close * 100, 2)
    base_conf = min(85, 45 + abs(total_score) * 8)

    # Penalize if range is too tight (fees would eat profit)
    if range_pct < CRYPTO_RULES["min_profit_pct"] * 100:
        base_conf = max(20, base_conf - 25)
        viable = False
    else:
        viable = True

    # Volume boost
    if ind.get("vol_ratio") and ind["vol_ratio"] > 1.3:
        base_conf = min(90, base_conf + 5)

    confidence = base_conf

    return {
        "symbol":     symbol,
        "close":      close,
        "proj_high":  proj_high,
        "proj_low":   proj_low,
        "pivot":      pivot,
        "atr":        round(atr, 4),
        "range_pct":  range_pct,
        "bias":       bias,
        "confidence": confidence,
        "viable":     viable,       # False = range too tight to trade profitably
        "total_score": total_score,
        "rsi":        rsi,
        "mom_24h":    ind.get("mom_24h"),
        "vol_ratio":  ind.get("vol_ratio"),
    }

def get_all_crypto_projections() -> dict:
    """
    Compute projections for all coins in universe.
    Returns {symbol: projection_dict}
    """
    projections = {}
    for symbol in CRYPTO_UNIVERSE:
        try:
            bars = get_crypto_bars(symbol, interval="1h", limit=168)
            ind  = compute_crypto_indicators(bars)
            proj = get_crypto_projection(symbol, bars, ind)
            projections[symbol] = proj
        except Exception as e:
            projections[symbol] = {"symbol": symbol, "error": str(e)}
    return projections

def format_crypto_projections_for_ai(projections: dict) -> str:
    """Format crypto projections as actionable AI prompt text — includes breakout signals."""
    lines = ["CRYPTO PROJECTIONS + BREAKOUT SIGNALS:"]
    for sym, proj in sorted(projections.items(),
                            key=lambda x: x[1].get("confidence", 0), reverse=True):
        if proj.get("error"):
            continue
        viable_tag = "✅ VIABLE" if proj.get("viable") else "⚠️ TIGHT"
        conf_tag   = "HIGH" if proj["confidence"] >= 70 else "MED" if proj["confidence"] >= 50 else "LOW"
        ind        = proj.get("indicators", {})
        breakout   = ind.get("breakout_signal", "")
        mom_score  = ind.get("momentum_score", 0)
        vol_spike  = ind.get("vol_spike", False)

        breakout_tag = ""
        if breakout == "BULLISH_BREAKOUT":
            breakout_tag = " 🚀 BREAKOUT+VOL"
        elif breakout == "BEARISH_BREAKDOWN":
            breakout_tag = " ⬇️ BREAKDOWN+VOL"
        elif breakout in ("BREAKOUT_NO_VOL",):
            breakout_tag = " ⚠️ BREAKOUT-no-vol"

        lines.append(
            f"  {sym}: ${proj['close']} | range ${proj['proj_low']}–${proj['proj_high']} "
            f"({proj['range_pct']:.1f}%) | {proj['bias'].upper()} | "
            f"conf={proj['confidence']} {conf_tag} | RSI={proj['rsi']} | "
            f"mom={mom_score}/100{breakout_tag} | {viable_tag}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# STAKING MANAGER
# Reads all staking positions, APY, rewards, unbonding periods
# AI decides: keep staking, unstake+trade, or stake more
# ══════════════════════════════════════════════════════════════

# Unbonding periods per coin (days) — approximate, set by each network
# Bot uses these to warn AI before recommending unstake
UNBONDING_PERIODS = {
    "ETH":   4,    # ~4 days
    "SOL":   3,    # ~3 days
    "DOT":   28,   # 28 days — very long!
    "ADA":   1,    # ~1 day
    "AVAX":  0,    # Instant
    "ATOM":  21,   # 21 days — long
    "MATIC": 3,    # ~3 days
    "BNB":   0,    # Instant
    "NEAR":  2,    # ~2 days
    "KAVA":  21,   # 21 days
    "ONE":   7,    # ~7 days
    "KSM":   7,    # ~7 days
    "XTZ":   0,    # Instant
    "VET":   0,    # Instant
    "SUI":   1,    # ~1 day
    "DOT":   28,   # 28 days
    "LPT":   7,    # ~7 days
    "ROSE":  14,   # 14 days
    "FET":   21,   # 21 days
}

def liquidate_all_to_usdt(log_fn=None) -> dict:
    """Liquidate all crypto positions to USDT."""
    def _log(msg):
        if log_fn:
            log_fn(f"[LIQUIDATE] {msg}")
        else:
            print(f"[LIQUIDATE] {msg}", flush=True)

    _log("💵 SPOT LIQUIDATION — converting free coins to USDT")
    _log("   (Staked assets kept — they stay earning APY)")
    _log("=" * 55)

    results = {
        "sold":        [],
        "skipped":     [],
        "failed":      [],
        "usdt_gained": 0.0,
        "usdt_final":  0.0,
        "status":      "ok",
    }

    import time as _time

    # ── Read wallet ───────────────────────────────────────────
    try:
        data     = binance_get("/api/v3/account", signed=True)
        balances = {b["asset"]: b for b in data["balances"]}
    except Exception as e:
        _log(f"❌ Cannot read wallet: {e}")
        results["status"] = "failed"
        return results

    # ── Skip list ─────────────────────────────────────────────
    skip_always = {"USDT", "BUSD", "USDC", "TUSD", "USDP", "BNB"}

    # Get staked asset names so we don't try to sell them
    staked_assets = set()
    try:
        staking = get_staking_info()
        for s in staking:
            if not s.get("error") and s.get("staked_qty", 0) > 0:
                staked_assets.add(s["asset"])
                _log(f"   🔒 {s['asset']}: staked {s['staked_qty']:.4f} "
                     f"= ${s.get('staked_value', 0):.2f} — KEEPING (earns APY)")
    except Exception as e:
        _log(f"   ⚠️ Could not read staking info: {e} — will skip no staked assets")

    _log("")

    # ── Sell all free spot balances ───────────────────────────
    for asset, bal in sorted(balances.items()):
        if asset in skip_always:
            continue

        free = float(bal.get("free", "0"))
        if free < 0.000001:
            continue

        sym = f"{asset}USDT"

        # Skip staked assets — their spot balance may be 0 anyway
        # but be explicit
        if asset in staked_assets:
            results["skipped"].append({"asset": asset, "reason": "staked"})
            continue

        # Get price
        price = 0.0
        try:
            price = get_crypto_price(sym)
        except Exception:
            pass

        if price <= 0:
            # Try 24hr ticker directly as last resort
            try:
                ticker = binance_get("/api/v3/ticker/24hr", {"symbol": sym})
                price  = float(ticker.get("lastPrice", 0))
            except Exception:
                pass

        if price <= 0:
            _log(f"   ⚠️ {asset}: no price found — skipping")
            results["skipped"].append({"asset": asset, "reason": "no_price",
                                        "qty": free})
            continue

        val = free * price
        if val < 1.0:
            _log(f"   ⚠️ {asset}: ${val:.4f} dust — skipping")
            results["skipped"].append({"asset": asset, "reason": "dust",
                                        "qty": free, "value": val})
            continue

        # Round qty to exchange step_size (prevents LOT_SIZE 400 errors)
        qty = _round_qty_step(free, sym)
        if qty <= 0:
            continue

        try:
            result = binance_post("/api/v3/order", {
                "symbol":    sym,
                "side":      "SELL",
                "type":      "MARKET",
                "quantity":  str(qty),
                "timestamp": _timestamp(),
            })

            if result.get("orderId"):
                usdt_est = round(qty * price, 2)
                _log(f"   ✅ SOLD {asset}: {qty} @ ${price:.8f} "
                     f"→ ~${usdt_est:.2f} USDT | order {result['orderId']}")
                results["sold"].append({
                    "asset":      asset,
                    "qty":        qty,
                    "price":      price,
                    "usdt_value": usdt_est,
                    "order_id":   result["orderId"],
                })
                results["usdt_gained"] += usdt_est
                _time.sleep(0.5)  # Rate limit protection
            else:
                _log(f"   ❌ SELL {asset} failed: {result}")
                results["failed"].append(f"sell_{asset}: {result}")
        except Exception as e:
            _log(f"   ❌ SELL {asset} error: {e}")
            results["failed"].append(f"sell_{asset}: {e}")

    # ── Read final USDT balance ───────────────────────────────
    try:
        data     = binance_get("/api/v3/account", signed=True)
        balances = {b["asset"]: b for b in data["balances"]}
        usdt_bal = float(balances.get("USDT", {}).get("free", 0))
        results["usdt_final"] = round(usdt_bal, 2)
    except Exception as e:
        _log(f"   ⚠️ Could not read final USDT: {e}")

    # ── Summary ───────────────────────────────────────────────
    _log("=" * 55)
    _log(f"✅ LIQUIDATION COMPLETE")
    _log(f"   Sold:       {len(results['sold'])} coins "
         f"→ ~${results['usdt_gained']:.2f} USDT")
    _log(f"   Skipped:    {len(results['skipped'])} "
         f"(staked/dust/no-price)")
    _log(f"   Final USDT: ${results['usdt_final']:.2f}")
    if results["failed"]:
        _log(f"   ⚠️ Failures: {results['failed']}")
    if staked_assets:
        _log(f"   🔒 Staked (untouched): {', '.join(staked_assets)}")
        _log(f"      → Still earning APY. Claim rewards from Binance.US → Earn")

    return results


def get_staking_info(asset: str = None) -> list:
    """Fetch staking balances and APY from Binance.US."""
    try:
        params = {}
        if asset:
            params["asset"] = asset
        data = binance_get("/sapi/v1/staking/asset", params, signed=True)

        results = []
        assets_data = data if isinstance(data, list) else data.get("assets", [])

        for a in assets_data:
            asset_name  = a.get("asset", "")
            staked_qty  = float(a.get("amount", 0))
            apy         = float(a.get("apy", 0))
            auto_stake  = a.get("autoRestake", True)
            pending     = float(a.get("rewardAmt", 0))

            if staked_qty < 0.000001:
                continue

            # Get current price for value calculation
            staked_value = 0.0
            try:
                price        = get_crypto_price(f"{asset_name}USDT")
                staked_value = round(staked_qty * price, 2)
            except Exception:
                pass

            unbonding = UNBONDING_PERIODS.get(asset_name, 7)  # Default 7 days

            # Calculate expected yields
            annual_yield = round(staked_value * apy / 100, 2) if staked_value else 0
            weekly_yield = round(annual_yield / 52, 4)

            results.append({
                "asset":             asset_name,
                "staked_qty":        staked_qty,
                "staked_value":      staked_value,
                "apy":               apy,
                "rewards_pending":   pending,
                "auto_restake":      auto_stake,
                "unbonding_days":    unbonding,
                "annual_yield_usdt": annual_yield,
                "weekly_yield_usdt": weekly_yield,
            })

        return sorted(results, key=lambda x: -x["staked_value"])

    except Exception as e:
        # Fallback: try alternate endpoint
        try:
            data = binance_get("/sapi/v1/staking/position", {} if not asset else {"asset": asset}, signed=True)
            results = []
            for a in (data if isinstance(data, list) else []):
                asset_name = a.get("asset", "")
                staked_qty = float(a.get("amount", a.get("qty", 0)))
                apy        = float(a.get("apy", a.get("apr", 0)))
                if staked_qty < 0.000001:
                    continue
                try:
                    price        = get_crypto_price(f"{asset_name}USDT")
                    staked_value = round(staked_qty * price, 2)
                except Exception:
                    staked_value = 0
                results.append({
                    "asset":             asset_name,
                    "staked_qty":        staked_qty,
                    "staked_value":      staked_value,
                    "apy":               apy,
                    "rewards_pending":   float(a.get("rewardAmt", 0)),
                    "auto_restake":      a.get("autoRestake", True),
                    "unbonding_days":    UNBONDING_PERIODS.get(asset_name, 7),
                    "annual_yield_usdt": round(staked_value * apy / 100, 2),
                    "weekly_yield_usdt": round(staked_value * apy / 100 / 52, 4),
                })
            return results
        except Exception as e2:
            return [{"error": f"Staking read failed: {e} / {e2}"}]

def get_staking_history(asset: str = None, days: int = 30) -> list:
    """Get recent staking reward history."""
    end   = int(time.time() * 1000)
    start = end - (days * 24 * 60 * 60 * 1000)
    params = {"startTime": start, "endTime": end}
    if asset:
        params["asset"] = asset
    try:
        data = binance_get("/sapi/v1/staking/history", params, signed=True)
        return data if isinstance(data, list) else data.get("history", [])
    except Exception:
        return []

def stake_asset(asset: str, qty: float) -> dict:
    """Stake additional coins."""
    return binance_post("/sapi/v1/staking/stake", {
        "asset":  asset,
        "amount": str(round(qty, 8)),
    })

def unstake_asset(asset: str, qty: float) -> dict:
    """Initiate unstaking — WARNING: unbonding period applies."""
    return binance_post("/sapi/v1/staking/unstake", {
        "asset":  asset,
        "amount": str(round(qty, 8)),
    })

def set_auto_restake(asset: str, enable: bool) -> dict:
    """Enable or disable auto-restaking for an asset."""
    return binance_post("/sapi/v1/staking/setAutoStaking", {
        "asset":       asset,
        "needAutoStaking": "true" if enable else "false",
    })

def format_staking_for_ai(staking_positions: list) -> str:
    """
    Format staking positions as actionable AI prompt text.
    Includes APY, value, rewards, and unstaking warnings.
    """
    if not staking_positions or staking_positions[0].get("error"):
        return "No staking positions found or staking data unavailable."

    lines = ["STAKING POSITIONS (earning passive yield):"]
    total_staked = sum(p.get("staked_value", 0) for p in staking_positions)
    total_weekly = sum(p.get("weekly_yield_usdt", 0) for p in staking_positions)

    lines.append(f"  Total staked: ${total_staked:.2f} | Est. weekly yield: ${total_weekly:.4f}")
    lines.append("")

    for p in staking_positions:
        if p.get("error"):
            continue
        unbond = p["unbonding_days"]
        unbond_warn = f"⚠️ {unbond}d to unstake" if unbond >= 7 else f"{unbond}d to unstake"
        auto_tag    = "🔄 auto-restake ON" if p["auto_restake"] else "⚡ auto-restake OFF"
        lines.append(
            f"  {p['asset']}: {p['staked_qty']:.4f} staked = ${p['staked_value']:.2f} | "
            f"APY={p['apy']:.2f}% | yield ~${p['weekly_yield_usdt']:.4f}/wk | "
            f"{unbond_warn} | {auto_tag}"
        )
        if p["rewards_pending"] > 0:
            lines.append(f"    → Pending rewards: {p['rewards_pending']:.6f} {p['asset']}")

    lines.append("")
    lines.append("STAKING DECISIONS FOR AI:")
    lines.append("  - KEEP STAKING: Good APY, no immediate trading opportunity → leave it")
    lines.append("  - UNSTAKE: Near proj_high + long unbond = BAD (price may drop during unbonding)")
    lines.append(f"  - UNSTAKE ONLY IF: price at proj_high AND unbonding ≤ 3 days AND APY < 5%")
    lines.append("  - STAKE MORE: Price at proj_low, good APY, expect sideways/up trend → add more")
    lines.append("  - AUTO-RESTAKE: Leave ON unless you need the reward coins for trading NOW")

    return "\n".join(lines)


class StakingManager:
    """
    Manages all staking decisions autonomously.
    Integrated into CryptoTrader — runs alongside trading cycle.
    """

    def __init__(self):
        self.last_check    = None
        self.decisions_log = []   # Log of all AI staking decisions
        self.check_interval_hours = 12  # Check staking every 12 hours

    def should_check(self) -> bool:
        """Only check staking every 12 hours — rewards distribute weekly."""
        if not self.last_check:
            return True
        hours = (datetime.now(timezone.utc) -
                 datetime.fromisoformat(self.last_check)).total_seconds() / 3600
        return hours >= self.check_interval_hours

    def run_staking_cycle(self, projections: dict,
                          ask_claude_fn, ask_grok_fn) -> dict:
        """
        Full AI staking review cycle.
        Reads all staking positions, asks AIs what to do,
        executes their decisions autonomously.
        """
        print(f"[STAKING] ── Staking Review Cycle ──", flush=True)

        # Read current staking positions
        staking_positions = get_staking_info()
        if not staking_positions or staking_positions[0].get("error"):
            print(f"[STAKING] ⚠️ Could not read staking positions", flush=True)
            return {}

        staking_text  = format_staking_for_ai(staking_positions)
        total_staked  = sum(p.get("staked_value", 0) for p in staking_positions)

        # Build projection context for staked assets
        proj_context = []
        for pos in staking_positions:
            asset  = pos["asset"]
            symbol = f"{asset}USDT"
            proj   = projections.get(symbol, {})
            if proj and not proj.get("error"):
                try:
                    curr     = get_crypto_price(symbol)
                    ph       = proj.get("proj_high", 0)
                    pl       = proj.get("proj_low", 0)
                    unbond   = pos["unbonding_days"]
                    if ph and pl:
                        dist_high = round((ph - curr) / curr * 100, 1)
                        at_high   = curr >= ph * 0.97
                        at_low    = curr <= pl * 1.03
                        proj_context.append(
                            f"  {asset}: curr=${curr:.4f} "
                            f"proj_low=${pl} proj_high=${ph} "
                            f"{'⚠️ AT PROJ HIGH' if at_high else f'{dist_high:.1f}% to proj_high'} | "
                            f"unbond={unbond}d | APY={pos['apy']:.1f}%"
                        )
                except Exception:
                    pass

        proj_text = "\n".join(proj_context) if proj_context else "  No projection data for staked assets"

        # Build AI prompt
        prompt = f"""=== STAKING PORTFOLIO REVIEW ===
Total value staked: ${total_staked:.2f}

{staking_text}

PRICE CONTEXT FOR STAKED ASSETS:
{proj_text}

STAKING STRATEGY RULES:
1. AUTO-RESTAKE is ON by default — LEAVE IT ON unless rewards coin is needed for trading
2. NEVER recommend unstaking if unbonding > 7 days unless price is at extreme high AND APY < 3%
3. DOT (28d unbond) and ATOM (21d unbond) — almost never worth unstaking for trading
4. AVAX, XTZ, VET have instant/near-instant unstaking — more flexible
5. Staking rewards are passive income — only disrupt if trading opportunity is significantly better
6. STAKE MORE only if coin is near proj_low AND trend is bullish AND you have free coins

DECISIONS NEEDED (for each staked asset):
- keep: continue current strategy
- stake_more: add X more coins (specify qty)
- unstake_partial: unstake X% (only if fast unbonding + good trading reason)
- unstake_full: unstake everything (only extreme cases — near proj_high + instant unbond)
- disable_auto_restake: turn off auto-restake (only if rewards needed immediately for trading)

JSON only:
{{"staking_decisions": [
  {{"asset": "SOL", "action": "keep",
    "reason": "brief", "confidence": 85}},
  {{"asset": "ETH", "action": "stake_more",
    "qty": 0.01, "reason": "near proj_low", "confidence": 75}},
  {{"asset": "AVAX", "action": "unstake_partial",
    "pct": 30, "reason": "near proj_high, instant unbond", "confidence": 70}}
],
"staking_note": "brief overall assessment"}}"""

        print(f"[STAKING] 🔵 Asking Claude for staking decisions...", flush=True)
        print(f"[STAKING] 🔴 Asking Grok for staking decisions...", flush=True)

        claude_resp = None
        grok_resp   = None

        try:
            claude_resp = ask_claude_fn(prompt,
                "You are Claude managing a staking portfolio. Prioritize passive yield. ONLY valid JSON.")
        except Exception as e:
            print(f"[STAKING] ⚠️ Claude staking failed: {e}", flush=True)

        try:
            grok_resp = ask_grok_fn(prompt,
                "You are Grok managing staking. Consider market momentum. ONLY valid JSON.")
        except Exception as e:
            print(f"[STAKING] ⚠️ Grok staking failed: {e}", flush=True)

        if not claude_resp and not grok_resp:
            print(f"[STAKING] ⚠️ Both AIs failed staking review", flush=True)
            self.last_check = datetime.now(timezone.utc).isoformat()
            return {}

        # ── Consolidate decisions — both AIs must agree on any action ──
        # "keep" is default — only act if both agree on something else
        claude_decisions = {d["asset"]: d for d in (claude_resp if isinstance(claude_resp, dict) else {}).get("staking_decisions", [])}
        grok_decisions   = {d["asset"]: d for d in (grok_resp   if isinstance(grok_resp,   dict) else {}).get("staking_decisions", [])}

        executed = {}
        all_assets = set(claude_decisions.keys()) | set(grok_decisions.keys())

        for asset in all_assets:
            c_dec = claude_decisions.get(asset, {})
            g_dec = grok_decisions.get(asset, {})
            c_act = c_dec.get("action", "keep")
            g_act = g_dec.get("action", "keep")

            # Find staking position for this asset
            pos = next((p for p in staking_positions if p["asset"] == asset), None)
            if not pos:
                continue

            unbond = pos["unbonding_days"]

            # Log what each AI said
            print(f"[STAKING] {asset}: Claude={c_act} | Grok={g_act}", flush=True)

            # Only execute if both agree on same non-keep action
            if c_act == g_act and c_act != "keep":
                action = c_act
                reason = c_dec.get("reason", g_dec.get("reason", ""))

                try:
                    if action == "unstake_partial":
                        pct = c_dec.get("pct", g_dec.get("pct", 25))
                        # Safety: never unstake if unbonding > 14 days
                        if unbond > 14:
                            print(f"[STAKING] ⚠️ BLOCKED unstake {asset} — "
                                  f"unbonding={unbond}d too long", flush=True)
                            continue
                        qty = round(pos["staked_qty"] * pct / 100, 8)
                        result = unstake_asset(asset, qty)
                        print(f"[STAKING] 🔓 Unstaked {pct}% of {asset} "
                              f"({qty:.6f}) — {reason}", flush=True)
                        executed[asset] = {"action": action, "qty": qty, "reason": reason}

                    elif action == "unstake_full":
                        # Extra safety: only allow if instant/fast unbonding
                        if unbond > 3:
                            print(f"[STAKING] ⚠️ BLOCKED full unstake {asset} — "
                                  f"unbonding={unbond}d > 3d safety limit", flush=True)
                            continue
                        result = unstake_asset(asset, pos["staked_qty"])
                        print(f"[STAKING] 🔓 FULL unstake {asset} — {reason}", flush=True)
                        executed[asset] = {"action": action,
                                           "qty": pos["staked_qty"], "reason": reason}

                    elif action == "stake_more":
                        qty = c_dec.get("qty", g_dec.get("qty", 0))
                        if qty > 0:
                            result = stake_asset(asset, qty)
                            print(f"[STAKING] 🔒 Staked more {asset}: "
                                  f"+{qty:.6f} — {reason}", flush=True)
                            executed[asset] = {"action": action,
                                               "qty": qty, "reason": reason}

                    elif action == "disable_auto_restake":
                        result = set_auto_restake(asset, False)
                        print(f"[STAKING] ⚡ Auto-restake DISABLED for "
                              f"{asset} — {reason}", flush=True)
                        executed[asset] = {"action": action, "reason": reason}

                except Exception as e:
                    print(f"[STAKING] ❌ Failed to execute {action} on {asset}: {e}",
                          flush=True)

            elif c_act != g_act:
                # AIs disagree — log but don't act (safety)
                print(f"[STAKING] 📌 {asset}: AIs disagree "
                      f"(Claude={c_act}, Grok={g_act}) — keeping as-is", flush=True)

            else:
                # Both say keep
                print(f"[STAKING] ✅ {asset}: Both AIs say KEEP staking "
                      f"(APY={pos['apy']:.1f}%)", flush=True)

        # Log notes
        for ai, resp in [("Claude", claude_resp), ("Grok", grok_resp)]:
            if resp and isinstance(resp, dict) and resp.get("staking_note"):
                print(f"[STAKING] {ai}: {resp['staking_note'][:100]}", flush=True)

        self.last_check = datetime.now(timezone.utc).isoformat()
        self.decisions_log.append({
            "time":     self.last_check,
            "executed": executed,
            "positions": len(staking_positions),
        })

        return executed

    def get_staking_summary(self) -> dict:
        """Return staking status for /crypto_status endpoint."""
        try:
            positions = get_staking_info()
            return {
                "positions":     positions,
                "total_staked":  sum(p.get("staked_value", 0) for p in positions),
                "total_weekly_yield": sum(p.get("weekly_yield_usdt", 0) for p in positions),
                "last_check":    self.last_check,
                "decisions_log": self.decisions_log[-5:],
            }
        except Exception as e:
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# ACCOUNT & POSITIONS
# ══════════════════════════════════════════════════════════════

def get_full_wallet() -> dict:
    """Fetch full wallet snapshot including all token balances."""
    try:
        data     = binance_get("/api/v3/account", signed=True)
        balances = {b["asset"]: b for b in data["balances"]}

        # ── Stablecoins ───────────────────────────────────────
        stablecoin_assets = ["USDT", "BUSD", "USDC", "TUSD", "USDP"]
        stablecoins = []
        usdt_free   = 0.0
        usdt_total  = 0.0

        for asset in stablecoin_assets:
            bal  = balances.get(asset, {})
            free = float(bal.get("free",   "0"))
            lock = float(bal.get("locked", "0"))
            if free + lock > 0.01:
                stablecoins.append({
                    "asset":      asset,
                    "free":       round(free, 4),
                    "locked":     round(lock, 4),
                    "value_usdt": round(free + lock, 4),
                })
            if asset == "USDT":
                usdt_free  = free
                usdt_total = free + lock

        # ── All non-zero crypto holdings ──────────────────────
        skip_assets = set(stablecoin_assets) | {"BNB"}  # BNB handled separately
        positions   = []
        tradeable   = []
        non_tradeable = []
        total_value = usdt_total

        for asset, bal in balances.items():
            if asset in skip_assets:
                continue
            free = float(bal.get("free",   "0"))
            lock = float(bal.get("locked", "0"))
            qty  = free + lock
            if qty < 0.000001:
                continue

            symbol = f"{asset}USDT"
            in_universe = symbol in CRYPTO_UNIVERSE

            try:
                price      = get_crypto_price(symbol)
                value_usdt = round(qty * price, 2)
                total_value += value_usdt

                entry = {
                    "asset":        asset,
                    "symbol":       symbol,
                    "qty":          qty,
                    "free":         free,
                    "locked":       lock,
                    "price":        price,
                    "value_usdt":   value_usdt,
                    "in_universe":  in_universe,
                }
                positions.append(entry)
                if in_universe:
                    tradeable.append(entry)
                else:
                    non_tradeable.append(entry)
            except Exception:
                # Price lookup failed (rebranded, delisted, no USDT pair)
                # Still show the holding so user knows it exists
                entry = {
                    "asset":       asset,
                    "symbol":      symbol,
                    "qty":         qty,
                    "free":        free,
                    "locked":      lock,
                    "price":       0,
                    "value_usdt":  0,
                    "in_universe": in_universe,
                    "note":        "no USDT price — check Binance.US",
                }
                positions.append(entry)
                if in_universe:
                    tradeable.append(entry)
                else:
                    non_tradeable.append(entry)

        # ── BNB (fee discount coin — track separately) ────────
        bnb_bal  = balances.get("BNB", {})
        bnb_qty  = float(bnb_bal.get("free", "0")) + float(bnb_bal.get("locked", "0"))
        bnb_info = None
        if bnb_qty > 0.0001:
            try:
                bnb_price = get_crypto_price("BNBUSDT")
                bnb_value = round(bnb_qty * bnb_price, 2)
                total_value += bnb_value
                bnb_info = {
                    "asset": "BNB", "qty": bnb_qty,
                    "price": bnb_price, "value_usdt": bnb_value,
                    "note": "fee discount coin — holds 5% fee savings",
                }
            except Exception:
                pass

        # ── Sort by value ──────────────────────────────────────
        positions     = sorted(positions,     key=lambda x: -x["value_usdt"])
        tradeable     = sorted(tradeable,     key=lambda x: -x["value_usdt"])
        non_tradeable = sorted(non_tradeable, key=lambda x: -x.get("value_usdt", 0))

        # ── Build wallet summary for AI prompt ────────────────
        lines = [f"BINANCE.US WALLET (total ~${total_value:.2f} USDT):"]
        lines.append(f"  💵 Spendable USDT: ${usdt_free:.2f} (locked: ${usdt_total-usdt_free:.2f})")
        if tradeable:
            lines.append(f"  🪙 Tradeable holdings ({len(tradeable)}):")
            for p in tradeable[:6]:
                lines.append(f"    {p['asset']}: {p['qty']:.4f} = ${p['value_usdt']:.2f} @ ${p['price']:.4f}")
        if non_tradeable:
            lines.append(f"  📦 Other holdings ({len(non_tradeable)}) — hold only:")
            for p in non_tradeable:
                if p.get("value_usdt", 0) > 0.5:
                    note = f" ({p['note']})" if p.get("note") else ""
                    lines.append(f"    {p['asset']}: {p['qty']:.4f} = ${p.get('value_usdt', 0):.2f}{note}")
                elif p.get("qty", 0) > 0:
                    note = p.get("note", "no USDT price — check Binance.US")
                    lines.append(f"    {p['asset']}: {p['qty']:.4f} ({note})")
        if bnb_info:
            lines.append(f"  🔶 BNB (fee coin): {bnb_info['qty']:.4f} = ${bnb_info['value_usdt']:.2f}")
        if stablecoins:
            other_stable = [s for s in stablecoins if s["asset"] != "USDT"]
            if other_stable:
                lines.append(f"  🏦 Other stables: " +
                             ", ".join(f"{s['asset']}=${s['value_usdt']:.2f}" for s in other_stable))

        wallet_summary = "\n".join(lines)

        return {
            "usdt_free":      round(usdt_free, 2),
            "usdt_total":     round(usdt_total, 2),
            "total_value":    round(total_value, 2),
            "positions":      positions,
            "stablecoins":    stablecoins,
            "tradeable":      tradeable,
            "non_tradeable":  non_tradeable,
            "bnb":            bnb_info,
            "position_count": len(positions),
            "wallet_summary": wallet_summary,
        }

    except Exception as e:
        return {
            "error": str(e),
            "usdt_free": 0, "usdt_total": 0, "total_value": 0,
            "positions": [], "stablecoins": [], "tradeable": [],
            "non_tradeable": [], "wallet_summary": "Wallet read failed",
        }

def get_crypto_balance() -> dict:
    """
    Backwards-compatible wrapper — returns USDT balance + positions.
    Calls get_full_wallet() internally.
    """
    wallet = get_full_wallet()
    return {
        "usdt_free":      wallet["usdt_free"],
        "usdt_total":     wallet["usdt_total"],
        "positions":      wallet["tradeable"],
        "position_count": len(wallet["tradeable"]),
    }

def get_open_crypto_orders(symbol: str = None) -> list:
    """Get all open orders, optionally filtered by symbol."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    try:
        return binance_get("/api/v3/openOrders", params, signed=True)
    except Exception:
        return []

def cancel_crypto_order(symbol: str, order_id: int) -> dict:
    """Cancel an open order."""
    return binance_delete("/api/v3/order", {"symbol": symbol, "orderId": order_id})


# ══════════════════════════════════════════════════════════════
# ORDER EXECUTION
# Always limit orders (0% maker fee)
# ══════════════════════════════════════════════════════════════

def _round_qty(qty: float, symbol: str) -> float:
    """Round quantity to correct decimal places for each coin."""
    decimals = CRYPTO_UNIVERSE.get(symbol, {}).get("decimals", 3)
    return round(qty, decimals)

def _round_qty_step(qty: float, symbol: str) -> float:
    """
    Round quantity DOWN to Binance exchange step_size.
    Uses get_symbol_filters() — same logic as place_crypto_buy.
    Fixes 400 LOT_SIZE errors on sell orders (e.g. DOTUSDT).
    """
    try:
        filters  = get_symbol_filters(symbol)
        step     = filters["step_size"]
        if step > 0:
            rounded = math.floor(qty / step) * step
            step_str  = f"{step:.10f}".rstrip('0')
            decimals  = len(step_str.split('.')[1]) if '.' in step_str else 0
            return float(f"{rounded:.{decimals}f}")
    except Exception:
        pass
    # Fallback to decimal-based rounding
    return _round_qty(qty, symbol)

def place_crypto_buy(symbol: str, notional_usdt: float,
                     limit_price: float = None) -> dict:
    """
    Place a MARKET buy order using quoteOrderQty (spend exact USDT).
    MARKET orders fill instantly — no stuck LIMIT orders.
    Falls back to qty-based MARKET if quoteOrderQty fails.
    """
    # Minimum notional check
    if notional_usdt < CRYPTO_RULES["min_trade_usdt"]:
        return {"error": f"Notional ${notional_usdt:.2f} below minimum $10"}

    # Method 1: quoteOrderQty — Binance spends exact USDT, handles qty internally
    # NOTE: not all Binance.US pairs support quoteOrderQty for MARKET BUY.
    # We detect success by the presence of 'orderId' in the response dict,
    # NOT by scanning for "error" substring (which can false-negative if
    # a valid response happens to contain an 'error' field or if the response
    # body is a string).
    try:
        result = binance_post("/api/v3/order", {
            "symbol":        symbol,
            "side":          "BUY",
            "type":          "MARKET",
            "quoteOrderQty": str(round(notional_usdt, 2)),
        })
        if isinstance(result, dict) and result.get("orderId"):
            return result
        # Otherwise fall through to Method 2
    except Exception:
        pass  # Fall through to method 2

    # Method 2: qty-based MARKET order (with safety buffer)
    # ── Apply 0.5% buffer so tiny price ticks between read and fill don't
    # ── trigger INSUFFICIENT_BALANCE 400 errors. Better to slightly
    # ── under-spend than to get rejected entirely.
    try:
        filters   = get_symbol_filters(symbol)
        step_size = filters["step_size"]
        price     = get_crypto_price(symbol)
        if price <= 0:
            return {"error": f"Cannot get price for {symbol}"}

        effective_notional = notional_usdt * 0.995  # 0.5% safety buffer
        raw_qty = effective_notional / price

        import math as _math
        if step_size > 0:
            qty_rounded = _math.floor(raw_qty / step_size) * step_size
            step_str = f"{step_size:.10f}".rstrip('0')
            decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
            qty = float(f"{qty_rounded:.{decimals}f}")
        else:
            qty = _round_qty(raw_qty, symbol)
        if qty <= 0:
            return {"error": f"Quantity rounded to 0 for {symbol}"}

        # Re-check notional: qty * price must still meet minimum
        est_notional = qty * price
        if est_notional < CRYPTO_RULES["min_trade_usdt"]:
            return {"error": f"Buffered qty notional ${est_notional:.2f} below minimum $10 for {symbol}"}

        return binance_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "BUY",
            "type":     "MARKET",
            "quantity": str(qty),
        })
    except Exception as e:
        return {"error": str(e)}

# ── Exchange filter cache (tick size / step size per symbol) ──────
_EXCHANGE_FILTERS: dict = {}   # {symbol: {"tick_size": float, "step_size": float, "min_notional": float}}

def get_symbol_filters(symbol: str) -> dict:
    """
    Fetch tick_size and step_size from Binance.US exchangeInfo.
    Cached per symbol — only fetches once per session.
    tick_size: minimum price increment (e.g. 0.00000001 for SHIB)
    step_size: minimum quantity increment
    """
    if symbol in _EXCHANGE_FILTERS:
        return _EXCHANGE_FILTERS[symbol]
    try:
        data = binance_get("/api/v3/exchangeInfo", {"symbol": symbol})
        info = data.get("symbols", [{}])[0]
        tick_size  = 0.00000001
        step_size  = 1.0
        min_notional = 10.0
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("minQty", 10)))
        result = {"tick_size": tick_size, "step_size": step_size,
                  "min_notional": min_notional}
        _EXCHANGE_FILTERS[symbol] = result
        return result
    except Exception:
        return {"tick_size": 0.00000001, "step_size": 1.0, "min_notional": 10.0}


def _round_to_tick(price: float, tick_size: float) -> str:
    """
    Round price DOWN to nearest tick_size increment.
    Returns string formatted to correct decimal places.
    E.g. SHIB tick_size=0.00000001 → price=0.00000621 → '0.00000621'
    """
    if tick_size <= 0:
        tick_size = 0.00000001
    import math
    rounded = math.floor(price / tick_size) * tick_size
    # Determine decimal places from tick_size
    tick_str   = f"{tick_size:.10f}".rstrip('0')
    if '.' in tick_str:
        decimals = len(tick_str.split('.')[1])
    else:
        decimals = 0
    return f"{rounded:.{decimals}f}"


def get_live_asset_balance(symbol: str) -> float:
    """
    Fetch the CURRENT free balance of the base asset from Binance.
    Critical before every SELL — tracked qty may differ from wallet qty
    due to commission fees deducted at buy time.
    
    Example: Buy 255.9 KAVA → Binance takes 1.5 KAVA as fee → wallet holds 254.39
    If bot tries to sell 255.9, Binance rejects with 400.
    
    Returns 0.0 on any failure (caller should handle).
    """
    try:
        # Extract base asset (KAVAUSDT → KAVA, BTCUSDT → BTC, etc.)
        base_asset = symbol.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
        if not base_asset:
            return 0.0
        data = binance_get("/api/v3/account", signed=True)
        balances = {b["asset"]: b for b in data.get("balances", [])}
        free = float(balances.get(base_asset, {}).get("free", 0))
        return free
    except Exception as e:
        print(f"[CRYPTO] ⚠️ get_live_asset_balance({symbol}) failed: {e}", flush=True)
        return 0.0


def reconcile_ghost_exit(symbol: str, entry_time) -> dict:
    """
    A tracked position's live Binance balance dropped to zero/dust without
    the bot selling it — almost always a broker-side STOP_LOSS order that
    filled directly on the exchange (survives bot restarts/redeploys by
    design). Query Binance's own fill history since entry to recover the
    real exit price/qty/fees so the loss can still be recorded, instead of
    the position just vanishing from trade history.

    Returns {} if no matching SELL fill is found (caller should fall back
    to an estimate using the current mark price rather than lose the
    trade entirely).
    """
    try:
        start_ms = int(entry_time.timestamp() * 1000)
        trades = binance_get("/api/v3/myTrades",
                              {"symbol": symbol, "startTime": start_ms, "limit": 100},
                              signed=True)
        sells = [t for t in (trades or [])
                 if not t.get("isBuyer") and float(t.get("qty", 0)) > 0]
        if not sells:
            return {}
        total_qty   = sum(float(t["qty"]) for t in sells)
        total_quote = sum(float(t["quoteQty"]) for t in sells)
        total_fee   = sum(float(t.get("commission", 0)) for t in sells
                           if t.get("commissionAsset") in ("USDT", "USD", "USDC"))
        if total_qty <= 0:
            return {}
        return {
            "qty":             total_qty,
            "avg_price":       total_quote / total_qty,
            "gross_proceeds":  total_quote,
            "fee_usd":         total_fee,
            "last_fill_time":  max(int(t["time"]) for t in sells),
        }
    except Exception as e:
        print(f"[CRYPTO] ⚠️ reconcile_ghost_exit({symbol}) failed: {e}", flush=True)
        return {}


def place_crypto_sell(symbol: str, qty: float,
                      limit_price: float = None,
                      force_limit: bool = False) -> dict:
    """
    Sell crypto. DEFAULTS TO MARKET ORDER — fills immediately at best bid.
    
    Per NOVATRADE_MASTER.md: "LIMIT orders on crypto cause PRICE_FILTER
    rejections — all sell paths use MARKET orders." Resting limits above
    market price sit unfilled, locking balance and getting re-stacked by
    every AI cycle. MARKET is the correct default.
    
    Pass force_limit=True only when you explicitly want a resting limit
    order (e.g. a TP ladder placed deliberately above market).
    
    CRITICAL: Fetches LIVE wallet balance before sending order.
    Binance takes commission at BUY time so wallet may hold LESS than
    the purchase amount. Selling the full purchase qty will fail with 400.
    """
    # ── Live balance check — prevent 400 errors from commission deductions ──
    live_balance = get_live_asset_balance(symbol)
    if live_balance > 0:
        # ── Dust check: if remaining balance is below min-notional, ────
        # ── treat it as ZERO. Otherwise we spam logs every cycle and
        # ── potentially get MIN_NOTIONAL 400s from Binance.
        try:
            cur_price = get_crypto_price(symbol)
            est_value = live_balance * cur_price if cur_price > 0 else 0
        except Exception:
            est_value = 0
        if 0 < est_value < 1.5:  # below $1.50 = dust (Binance min notional is $10)
            print(f"[CRYPTO]    🧹 {symbol}: dust balance {live_balance:.8f} "
                  f"(~${est_value:.4f}) — too small to sell, treating as zero", flush=True)
            return {"error": "DUST_BALANCE", "symbol": symbol,
                    "requested_qty": qty, "live_balance": live_balance,
                    "est_value": est_value}
        if live_balance < qty:
            print(f"[CRYPTO]    💡 {symbol}: requested sell qty {qty} > live balance {live_balance:.8f}", flush=True)
            print(f"[CRYPTO]       Using live balance (commission was deducted at buy)", flush=True)
            qty = live_balance * 0.9995  # 0.05% safety buffer for rounding
    elif live_balance == 0:
        print(f"[CRYPTO]    ⚠️ {symbol}: live balance = 0 — skipping sell (nothing to sell)", flush=True)
        return {"error": "ZERO_BALANCE", "symbol": symbol, "requested_qty": qty}

    qty_r = _round_qty_step(qty, symbol)
    if qty_r <= 0:
        return {"error": "QTY_ROUNDED_TO_ZERO", "symbol": symbol, "requested_qty": qty}

    # ── DEFAULT PATH: MARKET order (fills immediately) ──────────
    if not force_limit:
        return binance_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": qty_r,
        })

    # ── LIMIT path — only when caller explicitly requests it ────
    filters   = get_symbol_filters(symbol)
    tick_size = filters["tick_size"]

    if not limit_price or limit_price <= 0:
        try:
            limit_price = get_crypto_price(symbol) * 1.0015
        except Exception:
            # No price at all — fallback to MARKET
            return binance_post("/api/v3/order", {
                "symbol":   symbol,
                "side":     "SELL",
                "type":     "MARKET",
                "quantity": qty_r,
            })

    price_str = _round_to_tick(limit_price, tick_size)

    # Safety: never send price=0.0000...
    if float(price_str) <= 0:
        return binance_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": qty_r,
        })

    return binance_post("/api/v3/order", {
        "symbol":      symbol,
        "side":        "SELL",
        "type":        "LIMIT",
        "timeInForce": "GTC",
        "quantity":    qty_r,
        "price":       price_str,
    })

def place_crypto_stop_market(symbol: str, qty: float,
                              stop_price: float) -> dict:
    """
    Place a stop-loss market order for emergency exits.
    Used only when price hits stop — uses market order (0.01% taker fee).
    
    Like place_crypto_sell, fetches LIVE wallet balance first to prevent
    400 errors from commission-deducted holdings.
    """
    # ── Live balance check ──
    live_balance = get_live_asset_balance(symbol)
    if live_balance > 0:
        # Dust check (same threshold as place_crypto_sell)
        try:
            cur_price = get_crypto_price(symbol)
            est_value = live_balance * cur_price if cur_price > 0 else 0
        except Exception:
            est_value = 0
        if 0 < est_value < 1.5:
            print(f"[CRYPTO]    🧹 {symbol}: dust balance {live_balance:.8f} "
                  f"(~${est_value:.4f}) — too small to place stop", flush=True)
            return {"error": "DUST_BALANCE", "symbol": symbol,
                    "live_balance": live_balance, "est_value": est_value}
        if live_balance < qty:
            print(f"[CRYPTO]    💡 {symbol}: stop qty {qty} > live balance {live_balance:.8f} — using live balance", flush=True)
            qty = live_balance * 0.9995
    elif live_balance == 0:
        print(f"[CRYPTO]    ⚠️ {symbol}: live balance = 0 — cannot place stop", flush=True)
        return {"error": "ZERO_BALANCE", "symbol": symbol}
    
    qty     = _round_qty_step(qty, symbol)
    filters = get_symbol_filters(symbol)
    stop_str = _round_to_tick(stop_price, filters["tick_size"])
    return binance_post("/api/v3/order", {
        "symbol":    symbol,
        "side":      "SELL",
        "type":      "STOP_LOSS",
        "timeInForce": "GTC",
        "quantity":  qty,
        "stopPrice": stop_str,
    })


# ══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════

class CryptoPosition:
    """Tracks a single open crypto position with exit strategy."""

    def __init__(self, symbol, qty, entry_price, entry_time,
                 stop_pct=None, tp_price=None, owner="shared",
                 strategy_type="classic", turtle_system=1,
                 atr_at_entry=None, stop_price_override=None):
        self.symbol       = symbol
        self.qty          = qty
        self.entry_price  = entry_price
        self.entry_time   = entry_time
        self.stop_pct     = stop_pct or CRYPTO_RULES["stop_loss_pct"]
        # Turtle uses 2N stop (absolute price). Classic uses % stop.
        if stop_price_override is not None and stop_price_override > 0:
            self.stop_price = round(stop_price_override, 6)
        else:
            self.stop_price = round(entry_price * (1 - self.stop_pct), 4)
        self.tp_price     = tp_price or round(entry_price * (1 + CRYPTO_RULES["take_profit_pct"]), 4)
        self.owner        = owner
        self.peak_price   = entry_price
        self.exit_order_id = None
        # ── Turtle metadata ─────────────────────────────────────
        self.strategy_type = strategy_type      # "turtle" or "classic"
        self.turtle_system = turtle_system      # 1 (20/10) or 2 (55/20)
        self.atr_at_entry  = atr_at_entry       # N for 2N stop checks

    def update(self, current_price: float):
        """Update peak price for trailing logic."""
        if current_price > self.peak_price:
            self.peak_price = current_price

    def hours_held(self) -> float:
        """How many hours since entry."""
        delta = datetime.now(timezone.utc) - self.entry_time
        return delta.total_seconds() / 3600

    def pnl_pct(self, current_price: float) -> float:
        return round((current_price - self.entry_price) / self.entry_price * 100, 2)

    def should_stop(self, current_price: float) -> bool:
        # Works for both Classic (% stop_price) and Turtle (2N stop_price)
        return current_price <= self.stop_price

    def should_take_profit(self, current_price: float) -> bool:
        # Turtle has NO take-profit — only Donchian breakdown or 2N stop
        if self.strategy_type == "turtle":
            return False
        return current_price >= self.tp_price

    def should_time_exit(self) -> bool:
        # Turtle holds until exit signal — no time stop
        if self.strategy_type == "turtle":
            return False
        return self.hours_held() >= CRYPTO_RULES["max_hold_hours"]

    def should_turtle_donchian_exit(self) -> dict:
        """
        Check Donchian breakdown exit for Turtle positions.
        Returns {"should_exit": bool, "reason": str, "exit_level": float|None}.
        Returns inactive shape for non-Turtle positions.
        """
        if self.strategy_type != "turtle" or not self.atr_at_entry:
            return {"should_exit": False, "reason": "not turtle", "exit_level": None}
        try:
            return turtle_check_exit(self.symbol, self.entry_price,
                                    self.atr_at_entry, system=self.turtle_system)
        except Exception as e:
            return {"should_exit": False, "reason": f"check failed: {e}", "exit_level": None}

    def to_dict(self) -> dict:
        return {
            "symbol":       self.symbol,
            "qty":          self.qty,
            "entry_price":  self.entry_price,
            "stop_price":   self.stop_price,
            "tp_price":     self.tp_price,
            "owner":        self.owner,
            "peak_price":   self.peak_price,
            "hours_held":   round(self.hours_held(), 1),
            "strategy_type": self.strategy_type,
            "turtle_system": self.turtle_system,
            "atr_at_entry":  self.atr_at_entry,
        }


# ══════════════════════════════════════════════════════════════
# MAIN CRYPTO TRADER CLASS
# ══════════════════════════════════════════════════════════════


def get_funding_rates(symbols: list) -> dict:
    """
    Fetch perpetual futures funding rates from Binance.US.
    Free — we're already connected to Binance API.
    Funding rate interpretation:
      > +0.01% per 8h = longs paying shorts = crowded long = reversal risk
      < -0.01% per 8h = shorts paying longs = short squeeze potential
      Near 0 = balanced market, no strong bias
    Returns {symbol: {"rate": float, "signal": str}}
    """
    rates = {}
    for sym in symbols:
        try:
            perp_sym = sym if sym.endswith("USDT") else sym + "USDT"
            resp = binance_get(
                "/fapi/v1/fundingRate",
                {"symbol": perp_sym, "limit": 1}
            )
            if resp and isinstance(resp, list) and resp:
                rate = float(resp[0].get("fundingRate", 0)) * 100  # Convert to %
                if rate > 0.05:
                    signal = "HIGH_FUNDING — crowded longs, reversal risk"
                elif rate > 0.01:
                    signal = "ELEVATED_FUNDING — slight long bias"
                elif rate < -0.01:
                    signal = "NEGATIVE_FUNDING — shorts dominant, squeeze possible"
                else:
                    signal = "NEUTRAL_FUNDING — balanced market"
                rates[sym] = {"rate_pct": round(rate, 4), "signal": signal}
        except Exception:
            continue
    return rates

class CryptoTrader:
    """
    24/7 crypto trading engine for NovaTrade.
    Initialized once at module level in bot_with_proxy.py.
    Runs independently from stock trading cycle.
    """

    def __init__(self):
        self.positions      = {}
        self.trade_history  = []
        self.last_cycle     = None
        self.total_pnl      = 0.0
        self.wins           = 0
        self.losses         = 0
        self.cycle_count    = 0
        self._projections   = {}
        self._wallet_cache  = None   # Latest get_full_wallet() result — for BNB detection
        self._enabled       = bool(BINANCE_KEY and BINANCE_SECRET)
        self.staking        = StakingManager()
        self._shared_state  = None   # Injected by bot on init

        if self._enabled:
            self._log("🔐 Binance.US API keys found — crypto trading ENABLED")
            self._log("🔒 Staking manager initialized — reviews every 12 hours")
        else:
            self._log("⚠️ BINANCE_KEY or BINANCE_SECRET missing — crypto trading DISABLED")
            self._log("   Add BINANCE_KEY and BINANCE_SECRET to Railway variables")

    def update_crypto_baselines(self, wallet_value: float):
        """Update day/week/month/year start baselines on rollover."""
        if not self._shared_state:
            return
        ss = self._shared_state
        from datetime import datetime as _dt
        now       = _dt.now()
        cur_day   = now.strftime("%Y-%m-%d")
        cur_week  = now.strftime("%Y-W%W")
        cur_month = now.strftime("%Y-%m")
        cur_year  = now.strftime("%Y")

        if not ss.get("crypto_last_day"):
            ss["crypto_day_start"]   = wallet_value
            ss["crypto_week_start"]  = wallet_value
            ss["crypto_month_start"] = wallet_value
            ss["crypto_year_start"]  = wallet_value

        if ss.get("crypto_last_day") != cur_day:
            ss["crypto_day_start"]  = wallet_value
            ss["crypto_last_day"]   = cur_day
        if ss.get("crypto_last_week") != cur_week:
            ss["crypto_week_start"] = wallet_value
            ss["crypto_last_week"]  = cur_week
        if ss.get("crypto_last_month") != cur_month:
            ss["crypto_month_start"] = wallet_value
            ss["crypto_last_month"]  = cur_month
        if ss.get("crypto_last_year") != cur_year:
            ss["crypto_year_start"] = wallet_value
            ss["crypto_last_year"]  = cur_year

    def format_crypto_gains(self, wallet_value: float) -> str:
        """Return a one-line crypto gains summary."""
        if not self._shared_state:
            return ""
        ss = self._shared_state

        def _g(start):
            if not start or start <= 0:
                return "⬜ $+0.00 (+0.0%)"
            diff = wallet_value - start
            pct  = diff / start * 100
            icon = "🟢" if diff > 0 else "🔴" if diff < 0 else "⬜"
            return f"{icon} ${diff:+.2f} ({pct:+.1f}%)"

        d = ss.get("crypto_day_start",   wallet_value)
        w = ss.get("crypto_week_start",  wallet_value)
        m = ss.get("crypto_month_start", wallet_value)
        y = ss.get("crypto_year_start",  wallet_value)
        return (f"🪙 Gains  Day: {_g(d)}"
                f"  Week: {_g(w)}"
                f"  Month: {_g(m)}"
                f"  YTD: {_g(y)}")

    def get_ai_leaderboard(self, trade_history_ref=None) -> dict:
        """
        Tally crypto-only realized P&L by AI owner from trade history.
        Returns per-AI win rate, trade count, total P&L, and identifies
        the current leader. Used for both the cycle log line and the
        /leaderboard Flask endpoint.
        """
        # Use injected trade_history if provided, else try module-level
        if trade_history_ref is None:
            trade_history_ref = []

        # Crypto trades = symbol ends in USDT (or USDC/BUSD/USD pairs)
        # AND has a realized pnl_usd (closed position)
        def _is_crypto_close(t):
            sym = (t.get("symbol") or "").upper()
            return (t.get("pnl_usd") is not None
                    and sym.endswith(("USDT", "USDC", "BUSD"))
                    and t.get("action") in ("sell", "stop_loss", "take_profit",
                                            "trail_stop", "time_stop"))

        crypto_closes = [t for t in trade_history_ref if _is_crypto_close(t)]

        stats = {"claude": {"trades": 0, "wins": 0, "losses": 0,
                            "total_pnl": 0.0, "best": None, "worst": None,
                            "open_positions": 0},
                 "grok":   {"trades": 0, "wins": 0, "losses": 0,
                            "total_pnl": 0.0, "best": None, "worst": None,
                            "open_positions": 0},
                 "shared": {"trades": 0, "wins": 0, "losses": 0,
                            "total_pnl": 0.0, "best": None, "worst": None,
                            "open_positions": 0}}

        for t in crypto_closes:
            owner = (t.get("owner") or "shared").lower()
            if owner not in stats:
                continue
            pnl = float(t.get("pnl_usd", 0))
            stats[owner]["trades"]    += 1
            stats[owner]["total_pnl"] += pnl
            if pnl > 0:
                stats[owner]["wins"] += 1
            else:
                stats[owner]["losses"] += 1
            best = stats[owner]["best"]
            worst = stats[owner]["worst"]
            if best is None or pnl > float(best.get("pnl_usd", 0)):
                stats[owner]["best"]  = t
            if worst is None or pnl < float(worst.get("pnl_usd", 0)):
                stats[owner]["worst"] = t

        # Count open positions per owner from live state
        for sym, pos in self.positions.items():
            owner = (pos.owner or "shared").lower()
            if owner in stats:
                stats[owner]["open_positions"] += 1

        # Compute win rate + round
        for owner, s in stats.items():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0.0
            s["total_pnl"] = round(s["total_pnl"], 2)

        # Identify leader (Claude vs Grok only — "shared" doesn't compete)
        c_pnl = stats["claude"]["total_pnl"]
        g_pnl = stats["grok"]["total_pnl"]
        if stats["claude"]["trades"] == 0 and stats["grok"]["trades"] == 0:
            leader     = "tie"
            margin     = 0.0
            leader_emoji = "🤝"
        elif c_pnl > g_pnl:
            leader     = "claude"
            margin     = round(c_pnl - g_pnl, 2)
            leader_emoji = "🔵"
        elif g_pnl > c_pnl:
            leader     = "grok"
            margin     = round(g_pnl - c_pnl, 2)
            leader_emoji = "🔴"
        else:
            leader     = "tie"
            margin     = 0.0
            leader_emoji = "🤝"

        return {
            "claude":       stats["claude"],
            "grok":         stats["grok"],
            "shared":       stats["shared"],
            "leader":       leader,
            "leader_emoji": leader_emoji,
            "margin_usd":   margin,
            "total_closed": len(crypto_closes),
        }

    def format_leaderboard_line(self, trade_history_ref=None) -> str:
        """One-line leaderboard for cycle summary log."""
        lb = self.get_ai_leaderboard(trade_history_ref)
        c, g = lb["claude"], lb["grok"]

        def _ai_str(name, s):
            if s["trades"] == 0:
                return f"{name}: no closes yet ({s['open_positions']} open)"
            sign = "+" if s["total_pnl"] >= 0 else ""
            return (f"{name}: {sign}${s['total_pnl']:.2f} "
                    f"({s['wins']}W/{s['losses']}L, {s['win_rate']:.0f}% WR, "
                    f"{s['open_positions']} open)")

        leader_str = ""
        if lb["leader"] == "claude":
            leader_str = f" 👑 Claude leads by ${lb['margin_usd']:.2f}"
        elif lb["leader"] == "grok":
            leader_str = f" 👑 Grok leads by ${lb['margin_usd']:.2f}"
        elif c["trades"] + g["trades"] > 0:
            leader_str = " 🤝 Tied"

        return (f"🏆 Leaderboard | 🔵 {_ai_str('Claude', c)} | "
                f"🔴 {_ai_str('Grok', g)}{leader_str}")

    def _log(self, msg: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [CRYPTO] {msg}", flush=True)

    def is_enabled(self) -> bool:
        return self._enabled

    # ── CRYPTO POOL SIZING ──────────────────────────────────
    def get_crypto_pool(self, total_equity: float = 0) -> float:
        """
        How much USDT is available for crypto trading.
        Uses the ACTUAL Binance.US USDT balance — completely independent
        of Alpaca equity. The wallets are separate accounts.
        total_equity param kept for API compatibility but ignored.
        """
        try:
            wallet = get_full_wallet()
            return wallet.get("usdt_free", 0.0)
        except Exception:
            return 0.0

    # ── AUTONOMOUS EXIT MONITOR ─────────────────────────────
    def run_exit_monitor(self, record_trade_fn=None, prompt_builder=None) -> int:
        """
        Check all open positions for stop/TP/time exit + trail stop.
        Runs every 5-min tick — no AI needed.
        Trail stop activates at +30% gain, trails 40% from peak.
        Also auto-cancels stale unfilled orders (BUY >30min, SELL >60min).
        record_trade_fn: pass bot's record_trade so exits are saved to history.
        """
        if not self._enabled:
            return 0

        # ── Auto-cancel stale unfilled orders ─────────────────
        try:
            all_open = get_open_crypto_orders()
            for order in (all_open or []):
                side       = order.get("side", "")
                status     = order.get("status", "")
                symbol     = order.get("symbol", "")
                order_id   = order.get("orderId")
                order_time = order.get("time", 0)
                filled_qty = float(order.get("executedQty", 0))
                orig_qty   = float(order.get("origQty", 0))
                price      = float(order.get("price", 0))

                if status != "NEW" or filled_qty > 0:
                    continue

                # Protective STOP_LOSS orders rest at the broker by design
                # (survive bot outages) — never auto-cancel them as stale.
                if "STOP" in order.get("type", ""):
                    continue

                age_mins = (time.time() * 1000 - order_time) / 60000
                threshold = 30 if side == "BUY" else 60
                if age_mins < threshold:
                    continue

                try:
                    cancel_crypto_order(symbol, order_id)
                    self._log(
                        f"   🗑️ Cancelled stale {side} order: {symbol} "
                        f"{orig_qty} @ ${price:.6f} | age={age_mins:.0f}min"
                    )
                except Exception as ce:
                    self._log(f"   ⚠️ Cancel failed {symbol} {order_id}: {ce}")
        except Exception as oe:
            self._log(f"   ⚠️ Open order check failed: {oe}")

        if not self.positions:
            return 0

        # ── Quick-flip take-profit tier (computed once per cycle, not
        # per-position, to avoid extra API calls). Falls back to None
        # (no override — existing 8% tp_price stands) on any failure.
        quick_tp = None
        try:
            wallet_value = get_full_wallet().get("total_value", 0)
            quick_tp = get_crypto_quick_take_profit_pct(wallet_value)
        except Exception as we:
            self._log(f"   ⚠️ Quick-TP wallet lookup failed: {we}")

        exits = 0
        for symbol, pos in list(self.positions.items()):
            try:
                current = get_crypto_price(symbol)

                # ── Ghost position cleanup ───────────────────────
                # If wallet balance for this symbol is dust ($ < $1.50)
                # or zero, the position was sold outside the bot — almost
                # always a broker-side STOP_LOSS order filling directly on
                # Binance. Reconcile against Binance's own fill history so
                # the loss still lands in trade history/performance/the
                # AI's memory instead of vanishing (see reconcile_ghost_exit).
                try:
                    asset = symbol.replace("USDT", "")
                    live_qty = get_live_asset_balance(symbol)
                    live_val = live_qty * current if current > 0 else 0
                    if live_qty == 0 or 0 < live_val < 1.5:
                        recon = reconcile_ghost_exit(symbol, pos.entry_time)
                        if recon:
                            pnl_usd = round(recon["gross_proceeds"] - recon["fee_usd"]
                                             - pos.entry_price * recon["qty"], 2)
                            pnl_pct = round((recon["avg_price"] / pos.entry_price - 1) * 100, 2)
                            self._log(f"   🧹 {symbol}: ghost position — reconciled broker-side "
                                      f"exit @ ${recon['avg_price']:.6f} | P&L: ${pnl_usd:+.2f} "
                                      f"({pnl_pct:+.2f}%) — likely stop-loss fill")
                        else:
                            # No matching fill found on Binance — still record an
                            # ESTIMATE off the current mark rather than lose the
                            # trade silently. Clearly tagged as inexact.
                            pnl_usd = round((current - pos.entry_price) * pos.qty, 2)
                            pnl_pct = pos.pnl_pct(current)
                            self._log(f"   🧹 {symbol}: ghost position detected "
                                      f"(live qty={live_qty:.8f}, ~${live_val:.4f}) — no Binance "
                                      f"fill found, recording ESTIMATE P&L: ${pnl_usd:+.2f} "
                                      f"({pnl_pct:+.2f}%)")
                        if record_trade_fn:
                            try:
                                record_trade_fn(
                                    action       = "stop_loss",
                                    symbol       = symbol,
                                    qty          = recon.get("qty", pos.qty) if recon else pos.qty,
                                    price        = recon.get("avg_price", current) if recon else current,
                                    notional     = round((recon.get("avg_price", current) if recon else current)
                                                          * (recon.get("qty", pos.qty) if recon else pos.qty), 2),
                                    owner        = pos.owner,
                                    pnl_usd      = pnl_usd,
                                    pnl_pct      = pnl_pct / 100,
                                    strategy     = "crypto",
                                    entry_price  = pos.entry_price,
                                    reason       = "crypto:stop_loss (broker-side"
                                                   + ("" if recon else ", estimated") + ")",
                                )
                            except Exception as rte:
                                self._log(f"   ⚠️ ghost-exit record_trade failed: {rte}")
                        del self.positions[symbol]
                        continue
                except Exception as ge:
                    self._log(f"   ⚠️ Ghost-check failed {symbol}: {ge}")

                pos.update(current)
                pnl     = pos.pnl_pct(current)

                # ── Trail stop logic (CLASSIC only — Turtle uses Donchian/2N) ──
                trail_activate = CRYPTO_RULES["trail_activate_pct"]  # 30%
                trail_pct      = CRYPTO_RULES["trail_pct"]           # 40% from peak

                if pos.strategy_type != "turtle" and pnl >= trail_activate * 100:
                    # Trailing stop = peak × (1 - trail_pct)
                    trail_stop = round(pos.peak_price * (1 - trail_pct), 6)
                    if trail_stop > pos.stop_price:
                        old_stop = pos.stop_price
                        pos.stop_price = trail_stop
                        self._log(f"   📈 {symbol} trail stop: ${old_stop:.6f} → "
                                  f"${trail_stop:.6f} | peak=${pos.peak_price:.6f} "
                                  f"P&L={pnl:+.1f}%")

                exit_reason = None
                # ── Turtle Donchian breakdown check (Turtle positions only) ──
                # For Turtle positions, this fires before the 2N stop.
                # Donchian breakdown = trend ended → exit the winner.
                if pos.strategy_type == "turtle":
                    d_exit = pos.should_turtle_donchian_exit()
                    if d_exit.get("should_exit"):
                        exit_reason = f"turtle_exit ({d_exit.get('reason','')})"
                if exit_reason is None and pos.should_stop(current):
                    if pos.strategy_type == "turtle":
                        exit_reason = f"turtle_2N_stop ({pnl:.2f}%)"
                    else:
                        exit_reason = f"stop_loss ({pnl:.2f}%)"
                elif (exit_reason is None and pos.strategy_type != "turtle"
                      and quick_tp is not None and pnl >= quick_tp * 100):
                    # Small-wallet velocity override — bank any real gain
                    # above the tier floor instead of holding for 8%.
                    exit_reason = f"take_profit (quick tier {pnl:.2f}%)"
                elif exit_reason is None and pos.should_take_profit(current):
                    exit_reason = f"take_profit ({pnl:.2f}%)"
                elif exit_reason is None and pos.should_time_exit():
                    # ── Fee-floor guard ─────────────────────────────
                    # Don't dump on time_exit if we're underwater AND below
                    # fee floor — extend the hold instead. Past the hard cap,
                    # we exit anyway to avoid being stuck in a dead position.
                    fee_floor = pos.entry_price * (1 + CRYPTO_RULES["round_trip_fee"] + 0.005)
                    underwater = current < fee_floor
                    hours_held = pos.hours_held()
                    hard_cap   = CRYPTO_RULES.get("hard_max_hold_hours",
                                                   CRYPTO_RULES["max_hold_hours"] * 3)

                    if underwater and hours_held < hard_cap:
                        # Skip this exit — log once per hour to avoid spam
                        last_skip = getattr(pos, "_last_extend_log", 0)
                        if hours_held - last_skip >= 1.0:
                            self._log(f"   ⏳ {symbol}: time_exit skipped — "
                                      f"underwater ${current:.6f} < fee floor "
                                      f"${fee_floor:.6f} (P&L {pnl:+.2f}%) — "
                                      f"extending hold ({hours_held:.1f}h / {hard_cap}h cap)")
                            pos._last_extend_log = hours_held
                    else:
                        # Either above fee floor (exit OK to book breakeven+)
                        # or hard cap reached (force exit regardless of P&L)
                        cap_reason = " HARD CAP" if hours_held >= hard_cap else ""
                        exit_reason = (f"time_exit ({hours_held:.1f}h > "
                                       f"{CRYPTO_RULES['max_hold_hours']}h{cap_reason})")

                if exit_reason:
                    result = self._execute_exit(
                        pos, current, exit_reason,
                        record_trade_fn = record_trade_fn,
                        prompt_builder  = prompt_builder,
                    )
                    if result:
                        exits += 1
            except Exception as e:
                self._log(f"⚠️ Exit monitor error for {symbol}: {e}")

        return exits

    def _execute_exit(self, pos: CryptoPosition,
                      current_price: float, reason: str,
                      record_trade_fn=None,
                      prompt_builder=None) -> bool:
        """Execute a sell order and record the trade."""
        try:
            # Cancel any existing open orders for this symbol first
            open_orders = get_open_crypto_orders(pos.symbol)
            for order in open_orders:
                try:
                    cancel_crypto_order(pos.symbol, order["orderId"])
                except Exception:
                    pass

            # ── Fee-aware floor (informational) ───────────────
            # MARKET orders fill at current bid, so we can't enforce a
            # price floor at the exchange. Instead, log a warning if the
            # current market price is below entry + fees + 0.5%, since
            # this means we're likely booking a loss.
            min_sell = round(pos.entry_price * (1 + CRYPTO_RULES["round_trip_fee"] + 0.005), 6)
            if "stop_loss" not in reason and current_price < min_sell:
                self._log(f"   ⚠️ {pos.symbol}: MARKET exit at ${current_price:.4f} "
                          f"below fee floor ${min_sell:.4f} (entry ${pos.entry_price:.4f}) — "
                          f"reason: {reason}")

            # MARKET sell — place_crypto_sell defaults to MARKET, fills at current bid
            result = place_crypto_sell(pos.symbol, pos.qty)

            # ── Handle ghost positions — wallet empty/dust/un-sellable ─
            # ZERO_BALANCE: wallet has none of the asset
            # DUST_BALANCE: under $1.50 — too small to sell (Binance min notional $10)
            # QTY_ROUNDED_TO_ZERO: stepSize larger than balance — un-sellable
            ghost_errors = ("ZERO_BALANCE", "DUST_BALANCE", "QTY_ROUNDED_TO_ZERO")
            if isinstance(result, dict) and result.get("error") in ghost_errors:
                err = result.get("error")
                recon = reconcile_ghost_exit(pos.symbol, pos.entry_time)
                if recon:
                    pnl_usd = round(recon["gross_proceeds"] - recon["fee_usd"]
                                     - pos.entry_price * recon["qty"], 2)
                    pnl_pct = round((recon["avg_price"] / pos.entry_price - 1) * 100, 2)
                    exit_price = recon["avg_price"]
                    exit_qty   = recon["qty"]
                    self._log(f"   🧹 {pos.symbol}: ghost position ({err}) — reconciled "
                              f"broker-side exit @ ${exit_price:.6f} | P&L: ${pnl_usd:+.2f} "
                              f"({pnl_pct:+.2f}%)")
                else:
                    exit_price = current_price
                    exit_qty   = pos.qty
                    pnl_usd = round((current_price - pos.entry_price) * pos.qty, 2)
                    pnl_pct = pos.pnl_pct(current_price)
                    self._log(f"   🧹 {pos.symbol}: ghost position ({err}) — no Binance fill "
                              f"found, recording ESTIMATE P&L: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)")
                if record_trade_fn:
                    try:
                        record_trade_fn(
                            action       = reason.split("(")[0].strip(),
                            symbol       = pos.symbol,
                            qty          = exit_qty,
                            price        = exit_price,
                            notional     = round(exit_price * exit_qty, 2),
                            owner        = pos.owner,
                            pnl_usd      = pnl_usd,
                            pnl_pct      = pnl_pct / 100,
                            strategy     = "crypto",
                            entry_price  = pos.entry_price,
                            reason       = f"crypto:{reason} (ghost, "
                                           + ("reconciled)" if recon else "estimated)"),
                        )
                    except Exception as rte:
                        self._log(f"   ⚠️ ghost-exit record_trade failed: {rte}")
                del self.positions[pos.symbol]
                return True  # Treat as success to prevent infinite retries

            pnl_usd = round((current_price - pos.entry_price) * pos.qty, 2)
            pnl_pct = pos.pnl_pct(current_price)

            # ── Feed main trade history ───────────────────────
            if record_trade_fn:
                try:
                    action_type = reason.split("(")[0].strip()
                    record_trade_fn(
                        action       = action_type,
                        symbol       = pos.symbol,
                        qty          = pos.qty,
                        price        = current_price,
                        notional     = round(current_price * pos.qty, 2),
                        owner        = pos.owner,
                        pnl_usd      = pnl_usd,
                        pnl_pct      = pnl_pct / 100,
                        strategy     = "crypto",
                        entry_price  = pos.entry_price,
                        reason       = f"crypto:{reason}",
                    )
                except Exception as rte:
                    self._log(f"   ⚠️ record_trade failed: {rte}")

            # ── Feed prompt builder memory ────────────────────
            if prompt_builder:
                try:
                    prompt_builder.on_trade_closed(
                        symbol       = pos.symbol,
                        pnl_usd      = pnl_usd,
                        pnl_pct      = pnl_pct,
                        owner        = pos.owner,
                        strategy     = "crypto",
                        signals      = ["crypto", reason.split("(")[0].strip()],
                        entry_reason = f"crypto {pos.symbol} entry",
                    )
                except Exception as pbe:
                    self._log(f"   ⚠️ prompt_builder memory failed: {pbe}")

            # Internal trade log
            trade = {
                "symbol":       pos.symbol,
                "action":       reason.split("(")[0].strip(),
                "entry_price":  pos.entry_price,
                "exit_price":   current_price,
                "qty":          pos.qty,
                "pnl_usd":      pnl_usd,
                "pnl_pct":      pnl_pct,
                "hours_held":   round(pos.hours_held(), 1),
                "owner":        pos.owner,
                "time":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            self.trade_history.append(trade)
            if len(self.trade_history) > 200:
                self.trade_history.pop(0)

            self.total_pnl += pnl_usd
            if pnl_usd > 0:
                self.wins += 1
            else:
                self.losses += 1

            icon = "✅" if pnl_usd > 0 else "❌"
            self._log(f"{icon} EXIT {pos.symbol} | {reason} | "
                      f"P&L: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | "
                      f"held {trade['hours_held']}h")

            del self.positions[pos.symbol]
            return True

        except Exception as e:
            self._log(f"❌ Exit failed for {pos.symbol}: {e}")
            return False

    # ── AI COLLABORATION CYCLE ──────────────────────────────
    def run_crypto_cycle(self, total_equity: float,
                         ask_claude_fn, ask_grok_fn,
                         spy_trend: str = "neutral",
                         # ── New: full shared context ──────────────
                         prompt_builder=None,      # PromptBuilder instance
                         record_trade_fn=None,     # bot record_trade()
                         pol_text: str = "",       # Capitol Trades data
                         pol_mimick: list = None,  # Top politician mimick symbols
                         smart_money: dict = None, # Triple confirmation etc.
                         stock_projections: dict = None,  # Stock proj engine output
                         ) -> int:

        """Run one full crypto trading cycle: AI decisions + execution."""
        if not self._enabled:
            return 0

        self.cycle_count += 1
        self._log(f"── 🪙 Crypto Cycle #{self.cycle_count} ──")

        # Always run exit monitor first
        exits = self.run_exit_monitor()
        if exits:
            self._log(f"   {exits} position(s) exited autonomously")

        # Check if we can open new positions
        if len(self.positions) >= CRYPTO_RULES["max_positions"]:
            self._log(f"   Max positions ({CRYPTO_RULES['max_positions']}) reached — monitoring only")
            self._log_positions()
            return 0

        # Get available USDT + full wallet
        self._log("   💼 Reading full Binance.US wallet...")
        wallet = {}
        crypto_pool   = 0.0
        wallet_text   = ""
        tradeable     = []
        crypto_equity = 0.0
        try:
            wallet        = get_full_wallet()
            if wallet.get("error"):
                self._log(f"   ⚠️ Wallet error: {wallet['error']} — retrying once...")
                import time as _t; _t.sleep(2)
                wallet = get_full_wallet()  # One retry
                if wallet.get("error"):
                    self._log(f"   ❌ Wallet read failed twice: {wallet['error']}")
                    return 0

            # Cache the wallet so effective_fees() can detect BNB downstream
            self._wallet_cache = wallet
            if wallet.get("bnb"):
                _bnb_val = wallet["bnb"].get("value_usdt", 0)
                _eff = effective_fees(has_bnb=True)
                self._log(f"   🔶 BNB fee discount ACTIVE: holding ${_bnb_val:.2f} BNB "
                          f"→ effective round-trip fee {_eff['round_trip']*100:.3f}%")

            crypto_pool   = wallet["usdt_free"]
            wallet_text   = wallet["wallet_summary"]
            tradeable     = wallet["tradeable"]
            crypto_equity = wallet.get("total_value",
                            max(total_equity * 0.3, crypto_pool))

            # ── Log full wallet ──────────────────────────────────
            self._log(f"   💵 USDT free: ${crypto_pool:.2f} | "
                      f"Total wallet: ${crypto_equity:.2f}")

            # Show ONLY coins worth ≥ MIN_DISPLAY_VALUE — dust is collapsed
            visible_tradeable = [h for h in tradeable
                                 if h.get("value_usdt", 0) >= MIN_DISPLAY_VALUE]
            dust_tradeable    = [h for h in tradeable
                                 if h.get("value_usdt", 0) < MIN_DISPLAY_VALUE
                                 and h.get("qty", 0) > 0.000001]

            for h in visible_tradeable:
                price_str = (f"${h['price']:.8f}" if h['price'] < 0.001
                             else f"${h['price']:.4f}")
                self._log(f"   🪙 {h['asset']}: {h['qty']:.4f} "
                          f"= ${h['value_usdt']:.2f} @ {price_str}")

            # Show non-tradeable / held coins (FET, AUDIO, etc.)
            non_td = wallet.get("non_tradeable", [])
            visible = [p for p in non_td
                       if p.get("qty", 0) > 0
                       and p.get("value_usdt", 0) >= MIN_DISPLAY_VALUE]
            dust_non_td = [p for p in non_td
                           if p.get("qty", 0) > 0
                           and p.get("value_usdt", 0) < MIN_DISPLAY_VALUE]
            for p in visible[:6]:
                price_str = (f"${p['price']:.8f}" if p.get('price', 0) < 0.001
                             else f"${p['price']:.4f}")
                self._log(f"   📦 {p['asset']}: {p['qty']:.4f} "
                          f"= ${p['value_usdt']:.2f} @ {price_str}")

            # Single-line dust summary — keeps logs clean but still informs
            dust_all   = dust_tradeable + dust_non_td
            if dust_all:
                dust_total = sum(h.get("value_usdt", 0) for h in dust_all)
                dust_assets = ", ".join(h["asset"] for h in dust_all[:8])
                if len(dust_all) > 8:
                    dust_assets += f", +{len(dust_all)-8} more"
                self._log(f"   🧹 Dust: {len(dust_all)} coins ~${dust_total:.2f} "
                          f"({dust_assets}) — below ${MIN_DISPLAY_VALUE} threshold")

            # ── Show staked coins clearly ─────────────────────
            try:
                staking_positions = get_staking_info()
                if staking_positions and not staking_positions[0].get("error"):
                    for s in staking_positions:
                        rewards = s.get("rewards_pending", 0)
                        unbond  = s.get("unbonding_days", "?")
                        val     = s.get("staked_value", 0)
                        self._log(
                            f"   🔒 {s['asset']} STAKED: {s['staked_qty']:.4f} "
                            f"= ${val:.2f} | rewards={rewards:.4f} | "
                            f"unbond={unbond}d ← LOCKED, cannot sell directly"
                        )
            except Exception:
                pass

            if wallet.get("bnb"):
                bnb = wallet["bnb"]
                self._log(f"   🔶 BNB: {bnb['qty']:.4f} = ${bnb['value_usdt']:.2f}")

        except Exception as e:
            self._log(f"   ⚠️ Wallet read failed: {e}")
            return 0

        # Check if we have anything to work with
        # Either USDT to buy directly, OR coins we can sell first
        # Use $2 minimum for individual coins — they combine with existing USDT
        # NOTE: FET and other coins with price lookup failures still have qty
        # — we try to get their price live at sell time, not at filter time
        all_holdings = wallet.get("tradeable", []) + wallet.get("non_tradeable", [])
        min_sellable = 2.0

        def _estimate_value(h: dict) -> float:
            """Get best value estimate — try live price if stored is 0."""
            val = h.get("value_usdt", 0)
            if val > 0:
                return val
            # value=0 means price lookup failed at wallet read time
            # Try a fresh price lookup now
            sym = h.get("symbol", f"{h['asset']}USDT")
            qty = h.get("free", 0) + h.get("locked", 0)
            if qty <= 0:
                return 0
            try:
                price = get_crypto_price(sym)
                return round(qty * price, 2)
            except Exception:
                return 0

        sellable_coins = [h for h in all_holdings
                         if h.get("free", 0) > 0
                         and _estimate_value(h) >= min_sellable]

        total_available = crypto_pool + sum(_estimate_value(h) for h in sellable_coins)
        has_usdt   = crypto_pool >= CRYPTO_RULES["min_trade_usdt"]
        has_coins  = len(sellable_coins) > 0
        can_trade  = total_available >= CRYPTO_RULES["min_trade_usdt"]

        if not can_trade:
            self._log(f"   ⚠️ Nothing to trade — USDT=${crypto_pool:.2f} "
                      f"+ coins=${total_available - crypto_pool:.2f} "
                      f"= ${total_available:.2f} (min ${CRYPTO_RULES['min_trade_usdt']})")
            return 0

        # ── Tier-based sizing ─────────────────────────────────
        tier = get_crypto_tier(crypto_equity)
        risk_pct      = tier["risk_pct"]
        tier_max_pos  = tier["max_pos"]
        tier_coins    = tier["coins"]  # None = all universe unlocked
        trade_budget  = round(total_available * risk_pct, 2)
        CRYPTO_RULES["max_positions"] = tier_max_pos

        # ── Floor the suggested budget at the exchange minimum ──
        # A raw risk_pct slice of a small wallet can round below what
        # Binance.US will actually accept (e.g. 30% of $19.64 = $5.89
        # vs. a $10 minimum). If the wallet can afford the floor, ask
        # the AI for a legal order size instead of one it can't place.
        if (trade_budget < CRYPTO_RULES["min_order_usdt"]
                and total_available >= CRYPTO_RULES["min_order_usdt"]):
            self._log(f"   📐 Budget floored ${trade_budget:.2f} → "
                      f"${CRYPTO_RULES['min_order_usdt']:.2f} (wallet can afford the exchange minimum)")
            trade_budget = CRYPTO_RULES["min_order_usdt"]

        self._log(f"   📊 {tier['note']}")
        self._log(f"   💰 Risk per trade: {risk_pct*100:.0f}% = ${trade_budget:.2f} USDT")

        # ── Global drawdown check ─────────────────────────────
        # Pause all crypto trading if wallet dropped 40% from peak
        peak_val = self._peak_equity if hasattr(self, '_peak_equity') else crypto_equity
        if crypto_equity > peak_val:
            self._peak_equity = crypto_equity
            peak_val = crypto_equity
        drawdown = (peak_val - crypto_equity) / peak_val if peak_val > 0 else 0
        if drawdown >= CRYPTO_RULES["global_drawdown_pause"]:
            self._log(f"   🛑 DRAWDOWN PAUSE: wallet down {drawdown*100:.1f}% from peak "
                      f"${peak_val:.2f} → ${crypto_equity:.2f}. "
                      f"Pausing all trading until recovery.")
            return 0

        if not has_usdt and has_coins:
            coin_summary = [(h["asset"], f'${h["value_usdt"]:.2f}') for h in sellable_coins[:3]]
            self._log(f"   💡 No USDT but have sellable coins: {coin_summary}")
            self._log(f"   🔄 AI will decide: sell weak coins → buy stronger ones")

        # Get market data + crypto projections
        self._log("   📊 Computing crypto projections...")
        try:
            self._projections = get_all_crypto_projections()
            stats             = get_all_crypto_stats()
            # Scan full Binance.US market for top movers (not just our universe)
            market_scan       = scan_binance_market(min_volume_usdt=500_000, top_n=10)
        except Exception as e:
            self._log(f"   ⚠️ Market data failed: {e}")
            return 0

        proj_text  = format_crypto_projections_for_ai(self._projections)
        stats_text = [(s["symbol"], f"{s['change_pct']:+.2f}%",
                       f"vol={s['quote_volume']/1e6:.1f}M")
                      for s in stats[:8]]

        # Format market scan for AI — shows coins OUTSIDE our universe too
        scan_text = ""
        if market_scan:
            new_discoveries = [c for c in market_scan if not c["in_universe"]]
            scan_lines = ["MARKET SCAN — Top movers on Binance.US right now:"]
            for c in market_scan[:8]:
                tag = "★ NEW" if not c["in_universe"] else "  ·"
                scan_lines.append(
                    f"  {tag} {c['symbol']}: {c['change_pct']:+.1f}% "
                    f"@ ${c['price']} | vol=${c['volume_m']}M"
                )
            if new_discoveries:
                scan_lines.append(
                    f"\n  ⚡ {len(new_discoveries)} coins trending OUTSIDE our normal universe — "
                    f"AI can recommend buying any of these if setup looks good"
                )
            scan_text = "\n".join(scan_lines)

        # ── Crypto-specific situation classification ──────────
        # IMPORTANT: crypto uses its OWN classifier — not the stock one.
        # Stock P&L, SPY trend, and stock cash levels are IRRELEVANT to
        # crypto trading decisions. Crypto has its own pool (Binance USDT),
        # trades 24/7, and dip-buying is a valid strategy.
        situation_mode = "standard_monitoring"
        lessons_text   = ""

        try:
            # Classify based purely on crypto signals
            near_stop = [s for s, p in self.positions.items()
                         if p.pnl_pct(get_crypto_price(s)) <= -3.0]
            near_tp   = [s for s, p in self.positions.items()
                         if p.pnl_pct(get_crypto_price(s)) >= 4.0]
            crypto_pnl_pct = (self.total_pnl / max(crypto_equity, 1)
                              if self.total_pnl != 0 else 0.0)

            # Crypto situation modes — independent of stocks
            # Total sellable value = USDT + all free coin holdings
            total_sellable = crypto_pool + sum(
                (_estimate_value(h) if _estimate_value(h) > 0 else h.get("value_usdt", 0))
                for h in (wallet.get("tradeable", []) + wallet.get("non_tradeable", []))
                if h.get("free", 0) > 0 and (h.get("value_usdt", 0) >= 2.0 or
                    h.get("symbol", "") in _VERIFIED_SYMBOLS)
            )

            if near_stop:
                situation_mode = "defensive"
                focus = f"Crypto positions near stop: {near_stop}. Protect capital."
            elif crypto_pnl_pct <= -0.05:
                situation_mode = "damage_control"
                focus = f"Crypto P&L {crypto_pnl_pct*100:.1f}%. Review positions."
            elif near_tp:
                situation_mode = "harvest_profits"
                focus = f"Crypto positions near TP: {near_tp}. Lock in gains."
            elif total_sellable >= CRYPTO_RULES["min_trade_usdt"]:
                # Have USDT OR coins worth selling → can trade
                situation_mode = "opportunity_seeking"
                if crypto_pool >= CRYPTO_RULES["min_trade_usdt"]:
                    focus = f"${crypto_pool:.2f} USDT ready. Seek best setups."
                else:
                    focus = f"${total_sellable:.2f} in coins. Sell weak → buy strong."
            elif self.positions:
                situation_mode = "standard_monitoring"
                focus = "Managing open crypto positions."
            else:
                situation_mode = "capital_conservation"
                focus = "Insufficient funds to trade."

            self._log(f"   🧠 Crypto mode: {situation_mode.upper().replace('_',' ')} — {focus[:60]}")

            # Get learned lessons relevant to crypto
            if prompt_builder:
                lessons_text = prompt_builder.memory.format_for_prompt(
                    situation=situation_mode,
                    spy_trend=spy_trend,
                )
        except Exception as pe:
            self._log(f"   ⚠️ Crypto situation failed: {pe}")