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
CRYPTO_RULES = {
    # ── Stop / Profit targets (swing strategy) ───────────────
    # Crypto needs ROOM to move — tight stops get wiped by normal volatility
    "stop_loss_pct":        0.20,    # -20% stop (crypto swings 10-40% routinely)
    "take_profit_pct":      0.50,    # 50% TP base (trail once reached)
    "trail_activate_pct":   0.30,    # Start trailing at +30% gain
    "trail_pct":            0.40,    # Trail 40% from peak (lock 60% of gain)
    "min_profit_pct":       0.05,    # 5% minimum expected gain to enter
    "max_hold_hours":       72,      # 3-day time stop
    # ── Position sizing (tier-based) ─────────────────────────
    # More aggressive at small equity — needed to compound to goal
    "max_positions":        2,       # Max 2 positions at once
    "min_trade_usdt":       8.0,     # Binance.US minimum
    # ── USDT Reserve Strategy ────────────────────────────────
    "usdt_reserve_pct":     0.20,    # Keep 20% of wallet as USDT buffer
    "usdt_reserve_min":     10.0,    # Never drop below $10 USDT
    "usdt_quick_oppty":     15.0,    # $15+ USDT = ready for quick entry
    # ── Allocation Tiers (wallet size → position sizing) ─────
    # As wallet grows, diversify more conservatively
    "alloc_tiers": [
        (0,    0.45, 1, "Small: 45% max, 1 pos, BTC/SOL only"),
        (50,   0.35, 2, "Med: 35% max, 2 positions"),
        (100,  0.30, 3, "Large: 30% max, 3 positions"),
        (200,  0.25, 4, "XL: 25% max, 4 positions"),
    ],
    # ── Entry filters ─────────────────────────────────────────
    "min_confidence":       65,      # AI confidence threshold (lowered for more entries)
    "vol_spike_multiplier": 1.5,     # Volume must be 1.5x average to confirm breakout
    "breakout_periods":     20,      # 20-period high breakout trigger
    "rsi_momentum_min":     55,      # RSI must be above 55 for momentum entry
    "rsi_oversold_max":     35,      # RSI below 35 = oversold dip entry
    # ── Fees (Binance.US) ─────────────────────────────────────
    "maker_fee":            0.0000,
    "taker_fee":            0.0001,
    "round_trip_fee":       0.0002,
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
CRYPTO_TIERS = [
    {"min_equity":   0, "max_equity": 150,  "risk_pct": 0.45, "max_pos": 1,
     "coins": ["BTCUSDT", "SOLUSDT"],
     "note": "Tier 1 — 45% risk, 1 position, BTC/SOL only (highest vol)"},
    {"min_equity": 150, "max_equity": 300,  "risk_pct": 0.35, "max_pos": 2,
     "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
     "note": "Tier 2 — 35% risk, 2 positions, add ETH"},
    {"min_equity": 300, "max_equity": 600,  "risk_pct": 0.25, "max_pos": 2,
     "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT"],
     "note": "Tier 3 — 25% risk, add AVAX/ADA"},
    {"min_equity": 600, "max_equity": 9999, "risk_pct": 0.18, "max_pos": 3,
     "coins": None,  # All universe coins unlocked
     "note": "Tier 4 — 18% risk, full universe"},
]

def get_crypto_tier(wallet_value: float) -> dict:
    """Get current tier based on wallet total value."""
    for t in CRYPTO_TIERS:
        if t["min_equity"] <= wallet_value < t["max_equity"]:
            return t
    return CRYPTO_TIERS[-1]


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

def compute_crypto_volume_profile(bars: list, lookback: int = 504) -> dict:
    """
    3-week Price vs Volume analysis for crypto — identical math to stock version
    but calibrated for hourly bars.

    Lookback guide (hourly bars):
      168h = 1 week   — short-term momentum
      504h = 3 weeks  — institutional accumulation/distribution  <- default
      672h = 4 weeks  — full cycle view
    """
    if not bars or len(bars) < 24:
        return {"phase": "unknown", "prediction": "insufficient data", "conf": 0}

    bars = bars[-lookback:]
    n    = len(bars)

    closes = [float(b.get("c", 0)) for b in bars]
    highs  = [float(b.get("h", closes[i])) for i, b in enumerate(bars)]
    lows   = [float(b.get("l", closes[i])) for i, b in enumerate(bars)]
    vols   = [float(b.get("v", 0)) for b in bars]

    if not any(vols) or closes[-1] == 0:
        return {"phase": "unknown", "prediction": "no volume data", "conf": 0}

    # OBV + linear regression slope
    obv, obv_series = 0.0, []
    for i in range(n):
        if i == 0:                        obv += vols[i]
        elif closes[i] > closes[i-1]:     obv += vols[i]
        elif closes[i] < closes[i-1]:     obv -= vols[i]
        obv_series.append(obv)

    xs    = list(range(n))
    x_avg = sum(xs) / n
    y_avg = sum(obv_series) / n
    num   = sum((xs[i] - x_avg) * (obv_series[i] - y_avg) for i in range(n))
    den   = sum((xs[i] - x_avg) ** 2 for i in range(n))
    obv_slope      = num / den if den != 0 else 0.0
    avg_vol        = sum(vols) / n if n > 0 else 1
    obv_slope_norm = round(obv_slope / avg_vol * 100, 1)

    yp_avg      = sum(closes) / n
    num_p       = sum((xs[i] - x_avg) * (closes[i] - yp_avg) for i in range(n))
    price_slope = num_p / den if den != 0 else 0.0

    obv_rising   = obv_slope > 0
    price_rising = price_slope > 0

    if   price_rising and obv_rising:      obv_div = "confirming"
    elif price_rising and not obv_rising:  obv_div = "bearish"
    elif not price_rising and obv_rising:  obv_div = "bullish"
    else:                                  obv_div = "neutral"

    # VWAP over lookback window
    total_vol = sum(vols)
    typical   = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    vwap      = round(sum(t * v for t, v in zip(typical, vols)) / total_vol, 6) if total_vol > 0 else closes[-1]
    vwap_gap  = round((closes[-1] - vwap) / vwap * 100, 2) if vwap > 0 else 0
    # crypto uses 2% threshold (more volatile than stocks)
    price_vs_vwap = "above" if vwap_gap > 2.0 else "below" if vwap_gap < -2.0 else "at"

    # Buy/Sell volume ratio
    buy_vol  = sum(vols[i] for i in range(1, n) if closes[i] >= closes[i-1])
    sell_vol = sum(vols[i] for i in range(1, n) if closes[i] <  closes[i-1])
    bsr      = round(buy_vol / sell_vol, 2) if sell_vol > 0 else 1.5

    # Volume trend
    if n >= 8:
        fh        = sum(vols[:n//2]) / (n//2)
        sh        = sum(vols[n//2:]) / (n - n//2)
        vc        = (sh - fh) / fh if fh > 0 else 0
        is_climax = vols[-1] > avg_vol * 3.5  # higher threshold for crypto
        if is_climax:      vol_trend = "climax"
        elif vc >  0.30:   vol_trend = "expanding"
        elif vc < -0.30:   vol_trend = "contracting"
        else:              vol_trend = "normal"
    else:
        vol_trend = "normal"

    # Phase classification (6% coiling threshold vs 4% for stocks)
    pr_pct = (max(closes) - min(closes)) / min(closes) * 100 if min(closes) > 0 else 0

    if   vol_trend == "climax" and price_rising:        phase = "climax_top"
    elif vol_trend == "climax" and not price_rising:    phase = "exhaustion_bottom"
    elif obv_div == "bullish":                          phase = "accumulation"
    elif obv_div == "bearish":                          phase = "distribution"
    elif pr_pct < 6.0 and vol_trend == "contracting":  phase = "coiling"
    elif price_rising and obv_div == "confirming":      phase = "trending_up"
    elif not price_rising and obv_div == "neutral":     phase = "trending_down"
    else:                                               phase = "neutral"

    pred_map = {
        "accumulation":      ("Smart money accumulating quietly — breakout likely 1-2 weeks",     70),
        "distribution":      ("Whales distributing into retail — downside likely 1-2 weeks",      68),
        "coiling":           ("Energy coiling — large move imminent, watch volume for direction",  55),
        "trending_up":       ("Volume-confirmed uptrend — hold and buy dips near VWAP",           75),
        "trending_down":     ("Volume-confirmed downtrend — avoid longs, wait for exhaustion",    72),
        "climax_top":        (f"Blow-off top ({vols[-1]/avg_vol:.1f}x vol) — reversal likely",   78),
        "exhaustion_bottom": (f"Capitulation spike ({vols[-1]/avg_vol:.1f}x vol) — watch bounce", 65),
        "neutral":           ("No clear volume signal — wait for confirmation",                    35),
    }
    prediction, conf = pred_map.get(phase, ("No clear signal", 30))

    boosts = 0
    if obv_div in ("bullish", "confirming") and phase in ("accumulation", "trending_up"):  boosts += 1
    if price_vs_vwap == "below" and phase == "accumulation":                               boosts += 1
    if bsr > 1.3 and phase in ("accumulation", "trending_up"):                             boosts += 1
    if bsr < 0.7 and phase in ("distribution", "trending_down"):                           boosts += 1
    conf = min(92, conf + boosts * 8)

    return {
        "phase":          phase,
        "obv_slope_norm": obv_slope_norm,
        "obv_divergence": obv_div,
        "vwap":           vwap,
        "price_vs_vwap":  price_vs_vwap,
        "vwap_gap_pct":   vwap_gap,
        "buy_sell_ratio": bsr,
        "vol_trend":      vol_trend,
        "prediction":     prediction,
        "conf":           conf,
        "hours_used":     n,
    }


def get_all_crypto_projections() -> dict:
    """
    Compute projections for all coins in universe.
    Returns {symbol: projection_dict}
    """
    projections = {}
    for symbol in CRYPTO_UNIVERSE:
        try:
            # Fetch 3 weeks of hourly bars (504h) for volume profile
            # Falls back to 168h (1 week) if API limits hit
            bars_extended = get_crypto_bars(symbol, interval="1h", limit=504)
            bars          = bars_extended if len(bars_extended) >= 168 else                             get_crypto_bars(symbol, interval="1h", limit=168)
            ind  = compute_crypto_indicators(bars)
            proj = get_crypto_projection(symbol, bars, ind)

            # ── 3-week volume profile (predictive layer) ──────
            try:
                vp = compute_crypto_volume_profile(bars, lookback=min(504, len(bars)))
                proj["vol_profile"] = vp
            except Exception:
                proj["vol_profile"] = {}

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

        # ── Volume profile (3-week predictive layer) ─────────
        vp = proj.get("vol_profile", {})
        if vp and vp.get("phase") and vp["phase"] != "unknown":
            phase_icons = {
                "accumulation":      "🟢 ACCUM",
                "distribution":      "🔴 DISTRIB",
                "coiling":           "🟡 COILING",
                "trending_up":       "✅ TREND↑",
                "trending_down":     "❌ TREND↓",
                "climax_top":        "🚨 CLIMAX_TOP",
                "exhaustion_bottom": "🎯 EXHAUSTION",
                "neutral":           "⬜ NEUTRAL",
            }
            phase_icon = phase_icons.get(vp["phase"], vp["phase"].upper())
            hours      = vp.get("hours_used", 0)
            weeks      = round(hours / 168, 1)
            lines.append(
                f"    → VOL_PROFILE[{weeks}wk/{hours}h]: {phase_icon} | "
                f"VWAP=${vp['vwap']:.4f}({vp['vwap_gap_pct']:+.1f}%) | "
                f"B/S={vp['buy_sell_ratio']:.2f} | OBV={vp['obv_slope_norm']:+.0f}% | "
                f"vol={vp['vol_trend']} | conf={vp['conf']}%"
            )
            lines.append(f"    → FORECAST: {vp['prediction']} (conf={vp['conf']}%)")
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
    """
    Fetch all staking/earn positions from Binance.US.
    Queries THREE product types in order:
      1. Simple Earn Flexible  — /sapi/v1/simple-earn/flexible/position
         (Most common: auto-staking, FET, KAVA, AUDIO etc.)
      2. Simple Earn Locked    — /sapi/v1/simple-earn/locked/position
         (Fixed-term locked products)
      3. Legacy Locked Staking — /sapi/v1/staking/asset (fallback)
    Binance.US stores these in separate earn accounts — they do NOT
    appear in /api/v3/account spot balance at all.
    """
    def _make_entry(asset_name, staked_qty, apy, pending, auto_stake, product_type):
        """Build a normalized staking entry dict."""
        staked_value = 0.0
        try:
            price        = get_crypto_price(f"{asset_name}USDT")
            staked_value = round(staked_qty * price, 2)
        except Exception:
            pass
        unbonding    = 0 if product_type == "flexible" else UNBONDING_PERIODS.get(asset_name, 7)
        annual_yield = round(staked_value * apy / 100, 2) if staked_value else 0
        return {
            "asset":             asset_name,
            "staked_qty":        staked_qty,
            "staked_value":      staked_value,
            "apy":               apy,
            "rewards_pending":   pending,
            "auto_restake":      auto_stake,
            "unbonding_days":    unbonding,
            "product_type":      product_type,
            "annual_yield_usdt": annual_yield,
            "weekly_yield_usdt": round(annual_yield / 52, 4),
        }

    results = []
    seen    = set()  # deduplicate across endpoints

    # ── 1. Simple Earn Flexible (most common on Binance.US) ──────────
    try:
        params = {"size": 100}
        if asset:
            params["asset"] = asset
        data  = binance_get("/sapi/v1/simple-earn/flexible/position", params, signed=True)
        rows  = data if isinstance(data, list) else data.get("rows", data.get("data", []))
        for a in rows:
            asset_name = a.get("asset", a.get("productId", "").replace("USDT", "").rstrip("001"))
            staked_qty = float(a.get("totalAmount", a.get("amount", a.get("totalPersonalQuota", 0))))
            if not asset_name or staked_qty < 0.000001:
                continue
            if asset and asset_name != asset:
                continue
            apy        = float(a.get("latestAnnualPercentageRate", a.get("apy", a.get("apr", 0)))) * 100
            pending    = float(a.get("rewardAmt", a.get("totalRewards", 0)))
            auto_stake = a.get("autoSubscribe", a.get("autoRestake", True))
            key        = (asset_name, "flexible")
            if key not in seen:
                seen.add(key)
                results.append(_make_entry(asset_name, staked_qty, apy, pending, auto_stake, "flexible"))
    except Exception:
        pass  # Silently try next endpoint

    # ── 2. Simple Earn Locked ─────────────────────────────────────────
    try:
        params = {"size": 100}
        if asset:
            params["asset"] = asset
        data  = binance_get("/sapi/v1/simple-earn/locked/position", params, signed=True)
        rows  = data if isinstance(data, list) else data.get("rows", data.get("data", []))
        for a in rows:
            asset_name = a.get("asset", "")
            staked_qty = float(a.get("amount", a.get("principal", 0)))
            if not asset_name or staked_qty < 0.000001:
                continue
            if asset and asset_name != asset:
                continue
            apy        = float(a.get("apy", a.get("apr", 0)))
            pending    = float(a.get("rewardAmt", 0))
            auto_stake = a.get("autoSubscribe", False)
            key        = (asset_name, "locked")
            if key not in seen:
                seen.add(key)
                results.append(_make_entry(asset_name, staked_qty, apy, pending, auto_stake, "locked"))
    except Exception:
        pass

    # ── 3. Legacy Locked Staking (fallback) ──────────────────────────
    if not results:
        try:
            params = {}
            if asset:
                params["asset"] = asset
            data = binance_get("/sapi/v1/staking/asset", params, signed=True)
            rows = data if isinstance(data, list) else data.get("assets", [])
            for a in rows:
                asset_name = a.get("asset", "")
                staked_qty = float(a.get("amount", 0))
                if not asset_name or staked_qty < 0.000001:
                    continue
                if asset and asset_name != asset:
                    continue
                apy        = float(a.get("apy", 0))
                pending    = float(a.get("rewardAmt", 0))
                auto_stake = a.get("autoRestake", True)
                key        = (asset_name, "legacy")
                if key not in seen:
                    seen.add(key)
                    results.append(_make_entry(asset_name, staked_qty, apy, pending, auto_stake, "locked"))
        except Exception as e:
            try:
                data = binance_get("/sapi/v1/staking/position", {} if not asset else {"asset": asset}, signed=True)
                for a in (data if isinstance(data, list) else []):
                    asset_name = a.get("asset", "")
                    staked_qty = float(a.get("amount", a.get("qty", 0)))
                    if not asset_name or staked_qty < 0.000001:
                        continue
                    key = (asset_name, "legacy")
                    if key not in seen:
                        seen.add(key)
                        results.append(_make_entry(
                            asset_name, staked_qty,
                            float(a.get("apy", a.get("apr", 0))),
                            float(a.get("rewardAmt", 0)),
                            a.get("autoRestake", True), "locked"
                        ))
            except Exception as e2:
                if not results:
                    return [{"error": f"All staking endpoints failed: {e} / {e2}"}]

    return sorted(results, key=lambda x: -x["staked_value"])

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
                locked_note = f" (🔒 {p['locked']:.4f} staked)" if p.get('locked', 0) > 0.0001 else ""
                free_qty    = p.get('free', p['qty'])
                lines.append(f"    {p['asset']}: {free_qty:.4f} free{locked_note} = ${round(free_qty * p['price'], 2):.2f} tradeable @ ${p['price']:.4f}")
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
    Place a limit buy order with proper tick-size and step-size compliance.
    Fetches exchange filters to ensure price and quantity pass Binance filters.
    notional_usdt: how much USDT to spend
    """
    filters   = get_symbol_filters(symbol)
    tick_size = filters["tick_size"]
    step_size = filters["step_size"]

    if not limit_price or limit_price <= 0:
        try:
            limit_price = get_crypto_price(symbol) * 0.9985  # 0.15% below market
        except Exception:
            return {"error": f"Cannot get price for {symbol}"}

    # Minimum notional check
    if notional_usdt < CRYPTO_RULES["min_trade_usdt"]:
        return {"error": f"Notional ${notional_usdt:.2f} below minimum $10"}

    # Round price to tick size
    price_str = _round_to_tick(limit_price, tick_size)
    if float(price_str) <= 0:
        return {"error": f"Price rounded to 0 for {symbol} (tick_size={tick_size})"}

    # Round quantity DOWN to step size
    raw_qty = notional_usdt / float(price_str)
    import math as _math
    if step_size > 0:
        qty_rounded = _math.floor(raw_qty / step_size) * step_size
        # Format to correct decimal places from step_size
        step_str = f"{step_size:.10f}".rstrip('0')
        decimals  = len(step_str.split('.')[1]) if '.' in step_str else 0
        qty = float(f"{qty_rounded:.{decimals}f}")
    else:
        qty = _round_qty(raw_qty, symbol)

    if qty <= 0:
        return {"error": f"Quantity rounded to 0 for {symbol}"}

    return binance_post("/api/v3/order", {
        "symbol":      symbol,
        "side":        "BUY",
        "type":        "LIMIT",
        "timeInForce": "GTC",
        "quantity":    str(qty),
        "price":       price_str,
    })

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


def place_crypto_sell(symbol: str, qty: float,
                      limit_price: float = None) -> dict:
    """
    Place a limit sell order with proper tick-size compliant price.
    Fetches exchange filters to ensure price passes PRICE_FILTER.
    Falls back to MARKET order if price lookup fails.
    """
    filters = get_symbol_filters(symbol)
    tick_size = filters["tick_size"]

    if not limit_price or limit_price <= 0:
        try:
            limit_price = get_crypto_price(symbol) * 1.0015
        except Exception:
            # No price at all — use MARKET order
            qty_r = _round_qty_step(qty, symbol)
            return binance_post("/api/v3/order", {
                "symbol":    symbol,
                "side":      "SELL",
                "type":      "MARKET",
                "quantity":  qty_r,
            })

    qty_r      = _round_qty_step(qty, symbol)
    price_str  = _round_to_tick(limit_price, tick_size)

    # Safety: never send price=0.0000...
    if float(price_str) <= 0:
        qty_r = _round_qty_step(qty, symbol)
        return binance_post("/api/v3/order", {
            "symbol":    symbol,
            "side":      "SELL",
            "type":      "MARKET",
            "quantity":  qty_r,
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
    """
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
                 stop_pct=None, tp_price=None, owner="shared"):
        self.symbol       = symbol
        self.qty          = qty
        self.entry_price  = entry_price
        self.entry_time   = entry_time
        self.stop_pct     = stop_pct or CRYPTO_RULES["stop_loss_pct"]
        self.stop_price   = round(entry_price * (1 - self.stop_pct), 4)
        self.tp_price     = tp_price or round(entry_price * (1 + CRYPTO_RULES["take_profit_pct"]), 4)
        self.owner        = owner
        self.peak_price   = entry_price
        self.exit_order_id = None

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
        return current_price <= self.stop_price

    def should_take_profit(self, current_price: float) -> bool:
        return current_price >= self.tp_price

    def should_time_exit(self) -> bool:
        return self.hours_held() >= CRYPTO_RULES["max_hold_hours"]

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
        }


# ══════════════════════════════════════════════════════════════
# MAIN CRYPTO TRADER CLASS
# ══════════════════════════════════════════════════════════════

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
    def run_exit_monitor(self) -> int:
        """
        Check all open positions for stop/TP/time exit + trail stop.
        Runs every 5-min tick — no AI needed.
        Trail stop activates at +30% gain, trails 40% from peak.
        """
        if not self._enabled or not self.positions:
            return 0

        exits = 0
        for symbol, pos in list(self.positions.items()):
            try:
                current = get_crypto_price(symbol)
                pos.update(current)
                pnl     = pos.pnl_pct(current)

                # ── Trail stop logic ──────────────────────────
                trail_activate = CRYPTO_RULES["trail_activate_pct"]  # 30%
                trail_pct      = CRYPTO_RULES["trail_pct"]           # 40% from peak

                if pnl >= trail_activate * 100:
                    # Trailing stop = peak × (1 - trail_pct)
                    trail_stop = round(pos.peak_price * (1 - trail_pct), 6)
                    if trail_stop > pos.stop_price:
                        old_stop = pos.stop_price
                        pos.stop_price = trail_stop
                        self._log(f"   📈 {symbol} trail stop: ${old_stop:.6f} → "
                                  f"${trail_stop:.6f} | peak=${pos.peak_price:.6f} "
                                  f"P&L={pnl:+.1f}%")

                exit_reason = None
                if pos.should_stop(current):
                    exit_reason = f"stop_loss ({pnl:.2f}%)"
                elif pos.should_take_profit(current):
                    exit_reason = f"take_profit ({pnl:.2f}%)"
                elif pos.should_time_exit():
                    exit_reason = f"time_exit ({pos.hours_held():.1f}h > {CRYPTO_RULES['max_hold_hours']}h)"

                if exit_reason:
                    result = self._execute_exit(pos, current, exit_reason)
                    if result:
                        exits += 1
                        # ── Notify shared_state so stock AI wake can detect ──
                        if self._shared_state is not None:
                            if "stop_loss" in exit_reason or "time_exit" in exit_reason:
                                self._shared_state["crypto_stops_today"] = (
                                    self._shared_state.get("crypto_stops_today", 0) + 1
                                )
                            elif "take_profit" in exit_reason:
                                self._shared_state["crypto_tp_today"] = (
                                    self._shared_state.get("crypto_tp_today", 0) + 1
                                )
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

            sell_price = round(current_price * 1.005, 4)
            if "stop_loss" in reason:
                sell_price = pos.stop_price

            # ── Fee-aware floor: never sell below entry + fees ────
            # Crypto round-trip: 0.02% (0% maker + 0.01% taker × 2)
            # Minimum net profit: 0.5%
            # Floor = entry * (1 + 0.0002 + 0.005) = entry * 1.0052
            min_sell = round(pos.entry_price * (1 + CRYPTO_RULES["round_trip_fee"] + 0.005), 4)
            if "stop_loss" not in reason and sell_price < min_sell:
                self._log(f"   💰 {pos.symbol}: sell floored ${sell_price:.4f} → ${min_sell:.4f} "
                          f"(entry ${pos.entry_price:.4f} + fees + 0.5%)")
                sell_price = min_sell

            result = place_crypto_sell(pos.symbol, pos.qty, sell_price)

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

            crypto_pool   = wallet["usdt_free"]
            wallet_text   = wallet["wallet_summary"]
            tradeable     = wallet["tradeable"]
            crypto_equity = wallet.get("total_value",
                            max(total_equity * 0.3, crypto_pool))

            # ── Log full wallet ──────────────────────────────────
            self._log(f"   💵 USDT free: ${crypto_pool:.2f} | "
                      f"Total wallet: ${crypto_equity:.2f}")

            # ── Update shared_state so wake conditions can read crypto health ──
            if self._shared_state is not None:
                self._shared_state["crypto_last_wallet_value"] = crypto_equity
                self._shared_state["crypto_usdt_free"]         = crypto_pool

            # Show ALL tradeable coins
            for h in tradeable:
                price_str   = (f"${h['price']:.8f}" if h['price'] < 0.001
                               else f"${h['price']:.4f}")
                locked_qty  = h.get('locked', 0)
                free_qty    = h.get('free', h['qty'])
                staked_note = f" | 🔒 {locked_qty:.4f} staked (untouchable)" if locked_qty > 0.0001 else ""
                self._log(f"   🪙 {h['asset']}: {free_qty:.4f} free"
                          f"{staked_note} = ${round(free_qty * h['price'], 2):.2f} tradeable @ {price_str}")

            # Show non-tradeable / held coins (FET, AUDIO, etc.)
            non_td = wallet.get("non_tradeable", [])
            visible = [p for p in non_td if p.get("qty", 0) > 0]
            for p in visible[:6]:
                if p.get("value_usdt", 0) > 0.01:
                    price_str = (f"${p['price']:.8f}" if p.get('price', 0) < 0.001
                                 else f"${p['price']:.4f}")
                    self._log(f"   📦 {p['asset']}: {p['qty']:.4f} "
                              f"= ${p['value_usdt']:.2f} @ {price_str}")
                else:
                    note = p.get("note", "no price")
                    self._log(f"   📦 {p['asset']}: {p['qty']:.4f} ({note})")

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
                if crypto_pool >= CRYPTO_RULES["usdt_quick_oppty"]:
                    situation_mode = "opportunity_seeking"
                    reserve        = CRYPTO_RULES["usdt_reserve_min"]
                    deployable     = max(0, crypto_pool - reserve)
                    focus = (f"${crypto_pool:.2f} USDT ready. "
                             f"Keep ${reserve:.0f} USDT in reserve always. "
                             f"Deploy ${deployable:.2f} into best setup.")
                elif crypto_pool >= CRYPTO_RULES["min_trade_usdt"]:
                    situation_mode = "opportunity_seeking"
                    focus = (f"${crypto_pool:.2f} USDT — tight budget. "
                             f"Only trade HIGH confidence setups (conf>80). "
                             f"Consider selling weakest coin to build USDT first.")
                elif total_sellable > 15:
                    situation_mode = "rotation"
                    focus = (f"ZERO USDT — ROTATION MODE ACTIVE. "
                             f"${total_sellable:.2f} sitting idle in coins. "
                             f"MUST rotate: sell the WEAKEST holding → USDT → "
                             f"buy the STRONGEST breakout. Keep $10 USDT reserve.")
                else:
                    situation_mode = "opportunity_seeking"
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

        # ── Cross-reference stock projections for BTC-correlated stocks ──
        stock_cross_ref = ""
        if stock_projections:
            # BTC often leads NVDA, AMD, MSTR, COIN — check if they align
            correlated = ["NVDA", "AMD", "MSTR", "COIN"]
            cross_lines = []
            for sym in correlated:
                proj = stock_projections.get(sym, {})
                if proj and not proj.get("error"):
                    bias = proj.get("bias", "neutral")
                    conf = proj.get("confidence", 0)
                    if conf >= 55:
                        cross_lines.append(
                            f"  {sym}: {bias.upper()} conf={conf} "
                            f"(BTC-correlated stock signal)"
                        )
            if cross_lines:
                stock_cross_ref = ("CORRELATED STOCK SIGNALS "
                                   "(BTC leads these by 12-24h):\n" +
                                   "\n".join(cross_lines))

        # ── Politician signals on crypto-adjacent stocks ──────
        pol_section = ""
        if pol_text and pol_text.strip():
            # Filter for crypto-relevant stocks
            crypto_adjacent = ["COIN", "MSTR", "HOOD", "RIOT", "MARA",
                                "NVDA", "AMD"]
            pol_mentions = []
            for sym in crypto_adjacent:
                if sym in pol_text:
                    pol_mentions.append(sym)
            if pol_mentions or pol_mimick:
                pol_section = (
                    f"POLITICIAN TRADES (crypto-relevant):\n"
                    f"{pol_text[:300]}\n"
                    f"Crypto-adjacent stocks politicians are buying: "
                    f"{pol_mentions or 'none'}\n"
                    f"Top mimick symbols: {pol_mimick or []}"
                )

        # ── Smart money section ───────────────────────────────
        smart_section = ""
        if smart_money:
            triple = smart_money.get("triple_confirmation", [])
            # Check if any triple confirmation stocks are crypto-adjacent
            crypto_adj = {"COIN", "MSTR", "HOOD", "RIOT", "MARA", "NVDA"}
            crypto_triple = [s for s in triple if s in crypto_adj]
            if crypto_triple:
                smart_section = (
                    f"🔥 CRYPTO SIGNAL: Triple confirmation on "
                    f"crypto-adjacent stocks: {crypto_triple}\n"
                    f"→ This is a bullish signal for BTC/ETH"
                )

        # ── Binance fee constants ─────────────────────────────
        win_rate = round(self.wins / max(self.wins + self.losses, 1) * 100, 0)

        # Binance.US maker=0% taker=0.1% → use 0.1% round-trip to be safe
        BINANCE_FEE_RT = 0.001   # 0.1% round-trip (buy + sell)
        MIN_NET_PROFIT = 0.015   # 1.5% minimum net profit after fees

        # ── Staking summary for AI context ───────────────────
        staking_text = ""
        try:
            staking_positions = get_staking_info()
            if staking_positions and not staking_positions[0].get("error"):
                total_staked_val = sum(s.get("staked_value", 0) for s in staking_positions)
                staking_lines    = [f"\n🔒 STAKED COINS (LOCKED — cannot sell directly):"]
                for s in staking_positions:
                    rewards = s.get("rewards_pending", 0)
                    unbond  = s.get("unbonding_days", "?")
                    val     = s.get("staked_value", 0)
                    ptype = s.get("product_type", "locked")
                    ptype_label = "Simple Earn Flex" if ptype == "flexible" else "Simple Earn Locked"
                    unbond_note = "instant redeem" if s.get("unbonding_days", 7) == 0 else f"unbond={unbond}d"
                    staking_lines.append(
                        f"  {s['asset']}: {s['staked_qty']:.4f} = ${val:.2f} | "
                        f"[{ptype_label}] | pending rewards={rewards:.4f} | {unbond_note}"
                    )
                staking_lines.append(
                    f"  Total locked: ${total_staked_val:.2f} "
                    f"| To access: unstake (wait unbonding days) OR claim rewards only"
                )
                staking_lines.append(
                    f"  REWARDS are claimable immediately without unstaking!"
                )
                staking_text = "\n".join(staking_lines)
        except Exception:
            pass

        # ── Bot-tracked positions with P&L ───────────────────
        positions_text = ""
        if self.positions:
            positions_text = "\n🤖 BOT-TRACKED POSITIONS (must manage exits):\n"
            for sym, pos in self.positions.items():
                try:
                    curr_price = get_crypto_price(sym)
                    pnl_pct    = pos.pnl_pct(curr_price)
                    pnl_usd    = round((curr_price - pos.entry_price) * pos.qty, 4)
                    # Fee-aware minimum profitable exit
                    min_exit   = round(pos.entry_price * (1 + BINANCE_FEE_RT + MIN_NET_PROFIT), 6)
                    proj       = self._projections.get(sym, {})
                    dist_tp    = ""
                    if proj and not proj.get("error"):
                        ph = proj.get("proj_high", 0)
                        if ph:
                            dist_tp = f" | {round((ph-curr_price)/curr_price*100,1)}% to proj_high ${ph}"
                    positions_text += (
                        f"  {sym}: entry=${pos.entry_price:.6f} now=${curr_price:.6f} "
                        f"P&L={pnl_pct:+.1f}% (${pnl_usd:+.4f})\n"
                        f"    stop=${pos.stop_price:.6f} | TP=${pos.tp_price:.6f} "
                        f"| min_exit=${min_exit:.6f}{dist_tp}\n"
                    )
                except Exception:
                    positions_text += f"  {sym}: entry=${pos.entry_price:.6f} (price unavailable)\n"

        # ── Wallet holdings with fee-aware context ────────────
        holdings_text = ""
        all_wallet_holdings = tradeable + wallet.get("non_tradeable", [])

        if all_wallet_holdings:
            holdings_text = "\nWALLET HOLDINGS (decide: hold / sell-to-USDT / rotate):\n"
            for h in all_wallet_holdings:
                sym   = h.get("symbol", f"{h['asset']}USDT")
                proj  = self._projections.get(sym, {})
                val   = h.get("value_usdt", 0)
                price = h.get("price", 0)
                proj_note = ""

                if proj and not proj.get("error") and price > 0:
                    ph = proj.get("proj_high", 0)
                    pl = proj.get("proj_low", 0)
                    if ph and pl:
                        # Fee-aware minimum sell price
                        min_sell = round(price * (1 + BINANCE_FEE_RT + 0.005), 6)
                        if price >= ph * 0.98:
                            proj_note = f" ⚠️ AT PROJ HIGH — sell if ${ph} hit (profit-take)"
                        elif price <= pl * 1.02:
                            proj_note = f" 🟢 AT PROJ LOW — dip zone (good entry)"
                        else:
                            upside = round((ph - price) / price * 100, 1)
                            proj_note = f" → +{upside}% to proj_high ${ph} | min_sell=${min_sell}"

                if val > 0 or h.get("qty", 0) > 0:
                    locked_h  = h.get("locked", 0)
                    free_h    = h.get("free", h.get("qty", 0))
                    val_free  = round(free_h * price, 2) if price > 0 else 0
                    val_str   = f"= ${val_free:.2f}" if val_free > 0.01 else "(no price)"
                    price_str = (f"${price:.8f}" if price > 0 and price < 0.001
                                 else f"${price:.4f}" if price > 0 else "$0")
                    if locked_h > 0.0001:
                        staked_h_note = f" | 🔒 {locked_h:.4f} STAKED (locked, earns APY, not tradeable)"
                    else:
                        staked_h_note = ""
                    holdings_text += (f"  {h['asset']}: {free_h:.4f} FREE "
                                      f"{val_str} @ {price_str}"
                                      f"{proj_note}{staked_h_note}\n")

        # ── Determine trading mode ────────────────────────────
        no_buying_power = crypto_pool < CRYPTO_RULES["min_trade_usdt"] and not has_coins
        profit_focus    = no_buying_power and bool(self.positions)
        rotation_mode   = not has_usdt and has_coins

        if profit_focus:
            mode_instruction = """
🎯 PROFIT PROTECTION MODE — No buying power available.
PRIORITY: Protect and grow what you already have.
1. EXITS: Review all bot positions — are any near TP? Take profit if yes.
2. TRAIL: If position is profitable, raise stop to entry price (lock in breakeven minimum)
3. ROTATE only if a position has a clear sell signal AND a better coin is available
4. WATCH: Note best opportunities for when USDT becomes available"""
        elif rotation_mode:
            # Calculate which coin is weakest for rotation hint
            weakest_hint = ""
            try:
                scored = []
                for h in wallet.get("tradeable", []):
                    if h.get("value_usdt", 0) < 2: continue
                    sym   = f"{h['asset']}USDT"
                    proj  = self._projections.get(sym, {})
                    price = h.get("price", 0)
                    if proj and price > 0:
                        ph = proj.get("proj_high", price * 1.5)
                        pl = proj.get("proj_low", price * 0.8)
                        upside = (ph - price) / price if price > 0 else 0
                        scored.append((h["asset"], upside, h.get("value_usdt", 0)))
                if scored:
                    scored.sort(key=lambda x: x[1])  # lowest upside = weakest
                    weakest_hint = f"Weakest by upside: {scored[0][0]} (${scored[0][2]:.2f}, only {scored[0][1]*100:.1f}% to proj_high)"
            except Exception:
                pass
            mode_instruction = f"""
🔄 ROTATION MODE — USDT=$0.02, coins=${total_sellable:.2f} sitting idle.
IDLE CAPITAL IS WASTED CAPITAL. You MUST keep the portfolio working.

PRIORITY HIERARCHY:
1. SELL the WEAKEST holding first (lowest upside OR bearish signal OR near proj_high)
   {weakest_hint}
2. Keep $10 USDT reserve after selling — do NOT deploy all
3. BUY the STRONGEST current breakout with remaining USDT
4. New coin must have: proj_high upside >10% + RSI not overbought + volume signal

ROTATION CRITERIA — sell if ANY of these are true:
- Coin is within 2% of proj_high (take profit zone, run is almost over)
- RSI > 70 AND price declining (overbought reversal)
- Coin down 8%+ with bearish trend (cut losers)
- Better opportunity exists with 2x the remaining upside

DO NOT rotate if:
- ALL holdings still have >15% upside to proj_high (hold, wait for TP)
- The target coin to buy has no clear breakout signal
- Rotating would generate <$8 USDT (not enough to trade)

GROK: Use your live social + news intel to identify the STRONGEST momentum coin right now.
CLAUDE: Use technicals + projections to confirm the rotation target is valid."""
        else:
            reserve   = CRYPTO_RULES["usdt_reserve_min"]
            deployable = max(0, crypto_pool - reserve)
            mode_instruction = f"""
💰 OPPORTUNITY MODE — ${crypto_pool:.2f} USDT available.
RESERVE: Keep ${reserve:.0f} USDT always. Deploy ${deployable:.2f} maximum.

PRIORITY — find the BEST setup right now:
1. BREAKOUT FIRST: price just broke 20-period high + volume spike → BUY immediately
2. DIP BUY: RSI < 35 near proj_low → buy the oversold dip
3. MOMENTUM: trending coin with 5%+ upside to proj_high + bullish social sentiment

ENTRY RULES:
- Entry AT or BELOW proj_low = best risk/reward
- Avoid buying near proj_high (limited upside, high reversal risk)
- fee-aware: sell price must be > entry × 1.011 minimum

GROK — use your live intel right now:
- What coin is trending on X/Reddit in the last hour?
- Any whale accumulation signals?
- Any news catalyst driving a specific coin?
Share this with CLAUDE to align on the best trade.

CLAUDE — validate Grok's social picks with technical analysis:
- Does the trending coin have favorable RSI + volume?
- Is the price near proj_low (good entry) or proj_high (risky entry)?
- Confirm or reject Grok's recommendation with hard data.

BOTH must agree for execution. If you disagree, explain why in rationale."""

        # ── Build situation-aware system prompts ──────────────
        if prompt_builder:
            claude_system = prompt_builder.build_claude_system()
            grok_system   = prompt_builder.build_grok_system()
            claude_system = claude_system.replace(
                "ONLY valid JSON under 500 chars.",
                ("You are Claude doing technical crypto analysis for NovaTrade. "
                 "Your role: RSI, volume, projections, risk management, position sizing. "
                 "Grok provides live social/news intel — use it to validate or reject entries. "
                 "If Grok says a coin is trending on X with whale buys, check: "
                 "does the technical data confirm it? RSI not overbought? Near proj_low? "
                 "Disagree with Grok when technicals don't support the social hype. "
                 "Focus on 2-3 day momentum swings. ONLY valid JSON under 500 chars.")
            )
            grok_system = grok_system.replace(
                "ONLY valid JSON under 500 chars.",
                ("You have LIVE access to Twitter/X, Reddit, Telegram, CoinDesk, "
                 "CoinTelegraph, Whale Alert, Lookonchain, CoinGecko trending, "
                 "CoinMarketCap, and crypto forums. "
                 "ALWAYS search these sources for current sentiment and news catalysts. "
                 "Share what you find with Claude so you can align on the best trade. "
                 "Your role: social intelligence + news + on-chain signals. "
                 "Claude's role: technical analysis + projections + risk management. "
                 "ONLY valid JSON under 500 chars.")
            )
        else:
            claude_system = ("You are Claude analyzing crypto for NovaTrade. "
                             "ONLY valid JSON under 500 chars.")
            grok_system   = ("You are Grok analyzing crypto with Twitter/X "
                             "sentiment access. ONLY valid JSON under 500 chars.")

        # ── Step 1: Grok live research BEFORE prompt assembly ─
        grok_intel = ""
        try:
            self._log("   🔴 Grok searching X/web for crypto news...")
            watch_coins  = list(CRYPTO_UNIVERSE.keys())[:8]
            wallet_coins = [h["asset"] for h in
                            (wallet.get("tradeable", []) + wallet.get("non_tradeable", []))
                            if h.get("value_usdt", 0) > 1]
            position_coins = [s.replace("USDT","") for s in self.positions.keys()]
            all_watch = list(set(
                [s.replace("USDT", "") for s in watch_coins] + wallet_coins + position_coins
            ))[:10]

            research_prompt = (
                f"LIVE CRYPTO INTEL — search ALL sources NOW:\n"
                f"COINS: {', '.join(all_watch)}\n\n"
                f"1. Twitter/X: trending coins last 2hrs, whale alerts, "
                f"influencer calls, sentiment shift\n"
                f"2. Reddit (r/CryptoCurrency, r/CryptoMoonShots, r/Bitcoin): "
                f"top posts, new hype, FUD last 2hrs\n"
                f"3. News (CoinDesk, CoinTelegraph, Decrypt, The Block): "
                f"listings, hacks, partnerships, regulation\n"
                f"4. On-chain (Whale Alert, Lookonchain): whale moves >$500K, "
                f"exchange inflows/outflows, large liquidations\n"
                f"5. Macro: Fed signals, BTC ETF flows (Farside), DXY impact\n"
                f"6. CoinGecko/CMC trending tab: top gainers 1h + 24h\n"
                f"7. Telegram/Discord: credible pump signals or rug warnings\n"
                f"MARKET MOVERS: {[c['symbol'] for c in market_scan[:5]]}\n\n"
                f"5-8 bullets MAX. Format: **COIN +X%**: [source] catalyst — "
                f"bull/bear/neutral | sentiment 1-10 | momentum continuing? Y/N"
            )
            raw_intel = ask_grok_fn(
                research_prompt,
                "You are Grok with LIVE access to Twitter/X, Reddit, Telegram, "
                "CoinDesk, CoinTelegraph, Decrypt, Whale Alert, Lookonchain, "
                "CoinGecko and CoinMarketCap trending. Search ALL sources NOW. "
                "Focus on ACTIONABLE signals: what is moving RIGHT NOW and WHY. "
                "Include social sentiment score, whale activity, news catalyst. "
                "Plain text bullets only, no JSON.",
            )
            if raw_intel and len(raw_intel) > 20:
                grok_intel = raw_intel[:600]
                self._log(f"   🔴 Grok intel: {grok_intel[:200]}...")
        except Exception as e:
            self._log(f"   ⚠️ Grok research failed: {e}")

        # ── Assemble full prompt (grok_intel now defined) ─────
        # Find top breakout opportunities from projections
        breakout_coins = [
            f"{sym} 🚀" for sym, p in self._projections.items()
            if p.get("indicators", {}).get("breakout_signal") == "BULLISH_BREAKOUT"
        ]

        prompt = f"""=== CRYPTO SWING TRADING — NOVATRADE [{situation_mode.upper().replace('_',' ')}] ===
{wallet_text}

TIER: {tier['note']}
Available USDT: ${crypto_pool:.2f} | Wallet: ${crypto_equity:.2f} | Trade budget: ${trade_budget:.2f}
Crypto P&L: ${self.total_pnl:+.2f} | Win rate: {int(win_rate)}% ({self.wins}W/{self.losses}L)
{positions_text}
{holdings_text}
{staking_text}
SWING STRATEGY (aggressive compounding):
- STOP: {CRYPTO_RULES['stop_loss_pct']*100:.0f}% hard stop (crypto needs room)
- TP TARGET: 30-80% profit — NOT 5-10%. Big wins compound the account.
- TRAIL: activate at +{CRYPTO_RULES['trail_activate_pct']*100:.0f}%, trail {CRYPTO_RULES['trail_pct']*100:.0f}% from peak — lock in profits
- ENTRY: 20-period HIGH BREAKOUT + volume >{CRYPTO_RULES['vol_spike_multiplier']}x avg = strongest signal
- ENTRY ALT: RSI < {CRYPTO_RULES['rsi_oversold_max']} oversold near proj_low = dip buy
- SIZE: {risk_pct*100:.0f}% of wallet per trade = ${trade_budget:.2f}
- MAX POSITIONS: {tier_max_pos} at this tier
- TIME STOP: exit after {CRYPTO_RULES['max_hold_hours']}h regardless

💰 USDT RESERVE RULES (ALWAYS ENFORCE — NON-NEGOTIABLE):
- ALWAYS keep minimum $10 USDT in wallet — never deploy everything
- $10-$15 USDT = emergency reserve for flash opportunities
- Deploy USDT above the $10 reserve into best setup only
- If USDT < $10 → ROTATION required before ANY new buy

🔄 ROTATION MODE (when USDT is near zero):
- Idle coins = dead capital. You MUST keep capital working.
- Rank all holdings by: RSI momentum + distance to proj_high + 24h trend
- SELL the WEAKEST 1 holding → convert to USDT → buy STRONGEST breakout
- After rotation: keep $10 USDT reserve, deploy rest
- If ALL holdings are equally strong AND trending up → hold, wait for TP/stop to free USDT
- If any holding is DOWN 8%+ with bearish signal → sell it NOW, don't wait

📊 ALLOCATION RULES (grow responsibly):
- Wallet $0-$50:    max 45% per trade, 1 position only (BTC/SOL tier 1)
- Wallet $50-$100:  max 35% per trade, up to 2 positions
- Wallet $100-$200: max 30% per trade, up to 3 positions
- Wallet $200+:     max 25% per trade, up to 4 positions
- NEVER put >45% in one coin regardless of conviction
- Diversify as wallet grows — don't go all-in on one coin

🚀 BREAKOUT COINS RIGHT NOW: {breakout_coins or 'none detected yet'}
(Breakout = price just broke 20-period high + volume spike — highest priority entries)

FEE RULES:
- 0.1% round-trip → sell price must exceed entry × 1.001 minimum
- At 30-80% targets, fees are negligible (<0.1% of profit)

{mode_instruction}

24H MOVERS: {stats_text}

{scan_text}

{proj_text}

{stock_cross_ref}

{pol_section}

{smart_section}

{f"LEARNED CONTEXT:{chr(10)}{lessons_text}" if lessons_text else ""}

TASK — SWING TRADING [{situation_mode.upper().replace('_',' ')}]:
1. BREAKOUTS FIRST: Any coin showing BULLISH_BREAKOUT + volume spike? → BUY immediately
2. DIPS SECOND: RSI < {CRYPTO_RULES['rsi_oversold_max']} near proj_low? → BUY the dip
3. HOLD WINNERS: If position is up 20%+ and still bullish → hold toward 50-80% target
4. CUT LOSERS: Down 15%+ with bearish signal → sell, rotate into better setup
5. COIN-TO-COIN: Sell weakest coin → USDT → buy strongest breakout

PROFIT TARGET REMINDER:
  We need BIG wins to compound to goal. Do NOT exit at 5-10%.
  Target: 30-50% minimum per trade. Let winners run to 80%+ if trend holds.
  Use trail stop at +{CRYPTO_RULES['trail_activate_pct']*100:.0f}% to protect gains while staying in.

COIN-TO-COIN EXAMPLE:
  SHIB weak → sell_decisions: [{{"symbol": "SHIBUSDT", "reason": "weak, rotating to BTC breakout"}}]
  BTC breakout → crypto_trades: [{{"symbol": "BTCUSDT", "action": "buy", "notional_usdt": {trade_budget:.1f}, "confidence": 82, "entry_target": 0.0, "tp_target": 0.0}}]

JSON: {{"crypto_trades":[{{"symbol":"BTCUSDT","action":"buy","notional_usdt":{trade_budget:.1f},"confidence":80,"entry_target":95000.0,"tp_target":142000.0,"rationale":"breakout+vol"}}],"hold_decisions":[{{"symbol":"ETHUSDT","action":"hold","reason":"trending up, hold to 80% target"}}],"sell_decisions":[{{"symbol":"SOLUSDT","action":"sell","reason":"weak RSI, below 20-period low"}}],"avoid":["DOGEUSDT"],"market_note":"brief"}}

{f'GROK LIVE INTEL (from X/web search right now):{chr(10)}{grok_intel}' if grok_intel else ''}"""

        # ── Step 2: Ask both AIs with Grok intel included ─────
        self._log("   🔵 Claude analyzing crypto...")
        self._log("   🔴 Grok making final crypto decisions...")

        claude_resp = None
        grok_resp   = None

        def _parse_crypto_resp(raw):
            """Parse JSON from AI crypto response — robust against formatting issues."""
            if not raw:
                return None
            if isinstance(raw, dict):
                return raw  # Already parsed
            try:
                import json, re
                # Strip markdown fences (```json ... ``` or ``` ... ```)
                clean = re.sub(r'```(?:json)?\s*', '', str(raw)).replace('```', '').strip()
                # Remove trailing commas before } or ]
                clean = re.sub(r',\s*([}\]])', r'\1', clean)
                # Find outermost JSON object
                s = clean.find('{')
                e = clean.rfind('}') + 1
                if s < 0 or e <= s:
                    return None
                json_str = clean[s:e]
                # Try direct parse
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    # Try trimming truncated response
                    last_comma = json_str.rfind(',')
                    if last_comma > 0:
                        try:
                            result = json.loads(json_str[:last_comma] + '}')
                        except Exception:
                            result = None
                    else:
                        result = None
                if not result:
                    return None
                # Unwrap "analysis" wrapper if Claude used it
                if isinstance(result, dict) and "analysis" in result and len(result) == 1:
                    result = result["analysis"]
                return result
            except Exception:
                pass
            return None

        try:
            raw = ask_claude_fn(prompt, claude_system, 1500)
            claude_resp = _parse_crypto_resp(raw)
            if not claude_resp:
                self._log(f"   ⚠️ Claude crypto parse failed: {str(raw)[:100]}")
        except Exception as e:
            self._log(f"   ⚠️ Claude crypto failed: {e}")

        try:
            raw = ask_grok_fn(prompt, grok_system)
            grok_resp = _parse_crypto_resp(raw)
            if not grok_resp:
                self._log(f"   ⚠️ Grok crypto parse failed: {str(raw)[:100]}")
        except Exception as e:
            self._log(f"   ⚠️ Grok crypto failed: {e}")

        if not claude_resp and not grok_resp:
            self._log("   ⚠️ Both AIs failed — skipping crypto cycle")
            return 0

        # ── Process SELL decisions on ALL wallet holdings ─────
        # AI can sell any coin in wallet — not just bot-tracked positions
        # This allows converting weak holdings to USDT for better trades
        all_wallet = wallet.get("tradeable", []) + wallet.get("non_tradeable", [])
        wallet_map = {p["asset"]: p for p in all_wallet}  # asset → holding

        sell_decisions = []
        for ai_name, resp in [("claude", claude_resp), ("grok", grok_resp)]:
            if not resp or not isinstance(resp, dict):
                continue
            for sell in resp.get("sell_decisions", []):
                sym = sell.get("symbol", "")
                if sym:
                    sell_decisions.append((sym, sell.get("reason", "AI recommendation"), ai_name))

        # Execute sells — both AIs agree OR single AI for weak/small coins
        sell_counts = {}
        for sym, reason, ai in sell_decisions:
            sell_counts[sym] = sell_counts.get(sym, [])
            sell_counts[sym].append((reason, ai))

        for sym, decisions in sell_counts.items():
            both_agree    = len(decisions) >= 2
            proj          = self._projections.get(sym, {})
            near_proj_high = False
            if proj and not proj.get("error"):
                try:
                    curr = get_crypto_price(sym)
                    ph   = proj.get("proj_high", 0)
                    if ph and curr >= ph * 0.99:
                        near_proj_high = True
                except Exception:
                    pass

            # Single-AI sell allowed for small/weak coins — coins worth < $20
            # with no strong projection that have been flagged for rotation
            asset      = sym.replace("USDT", "")
            holding    = wallet_map.get(asset, {})
            coin_value = holding.get("value_usdt", 0) or holding.get("free", 0) * holding.get("price", 0)
            weak_coin  = coin_value < 20.0 and not near_proj_high

            if both_agree or near_proj_high or (len(decisions) == 1 and weak_coin):
                sell_tag = ('both AIs' if both_agree
                            else 'near proj_high' if near_proj_high
                            else f'single AI ({decisions[0][1]}) weak coin')
                try:
                    asset = sym.replace("USDT", "")
                    holding = wallet_map.get(asset)
                    if holding and holding.get("free", 0) > 0:
                        qty = holding["free"]

                        # Get price — fallback to wallet stored price for micro-price coins
                        curr = 0.0
                        try:
                            curr = get_crypto_price(sym)
                        except Exception:
                            pass
                        if curr <= 0:
                            curr = holding.get("price", 0)
                            if curr > 0:
                                self._log(f"   ℹ️ {sym} using wallet price ${curr:.8f}")

                        val = qty * curr if curr > 0 else 0

                        # Skip dust
                        if curr > 0 and val < 2.0:
                            self._log(f"   ⚠️ {sym} dust (${val:.4f}) — skipping sell")
                            continue

                        # Round qty to exchange step_size (prevents LOT_SIZE 400 errors)
                        qty = _round_qty_step(qty, sym)
                        if qty <= 0:
                            self._log(f"   ⚠️ {sym} rounded to 0 qty — skipping")
                            continue

                        if curr > 0:
                            # LIMIT sell with tick-size compliant price
                            filters   = get_symbol_filters(sym)
                            sell_price = _round_to_tick(curr * 1.001, filters["tick_size"])
                            if float(sell_price) <= 0:
                                # Fallback to MARKET
                                result = binance_post("/api/v3/order", {
                                    "symbol":    sym,
                                    "side":      "SELL",
                                    "type":      "MARKET",
                                    "quantity":  str(qty),
                                    "timestamp": _timestamp(),
                                })
                            else:
                                # MARKET sell — instant fill, avoids PRICE_FILTER 400 errors
                                qty_ms = _round_qty_step(qty, sym)
                                result = binance_post("/api/v3/order", {
                                    "symbol":    sym,
                                    "side":      "SELL",
                                    "type":      "MARKET",
                                    "quantity":  str(qty_ms),
                                    "timestamp": _timestamp(),
                                })
                        else:
                            # No price available — MARKET sell
                            self._log(f"   ⚠️ {sym} no price — using MARKET sell")
                            result = binance_post("/api/v3/order", {
                                "symbol":    sym,
                                "side":      "SELL",
                                "type":      "MARKET",
                                "quantity":  str(qty),
                                "timestamp": _timestamp(),
                            })

                        if result.get("orderId"):
                            usdt_est = round(qty * curr, 2) if curr > 0 else 0
                            self._log(f"   ✅ Sell order: {sym} {qty} → ~${usdt_est:.2f} USDT | "
                                      f"order={result['orderId']}")
                        else:
                            self._log(f"   ⚠️ Sell failed: {result}")
                    elif holding and holding.get("locked", 0) > 0:
                        self._log(f"   ⚠️ {asset} is locked/staked — cannot sell "
                                  f"({holding['locked']:.4f} locked)")
                    else:
                        self._log(f"   ⚠️ {asset} not found in wallet or zero balance")
                except Exception as e:
                    self._log(f"   ❌ Sell error for {sym}: {e}")

        # ── Log hold decisions ─────────────────────────────────
        for ai_name, resp in [("claude", claude_resp), ("grok", grok_resp)]:
            if not resp or not isinstance(resp, dict):
                continue
            for hold in resp.get("hold_decisions", []):
                sym    = hold.get("symbol", "")
                reason = hold.get("reason", "")
                if sym:
                    self._log(f"   📌 {ai_name.title()} says HOLD {sym}: {reason[:60]}")

        # Extract and validate trade proposals
        new_positions = 0
        proposals = []

        for ai_name, resp in [("claude", claude_resp), ("grok", grok_resp)]:
            if not resp or not isinstance(resp, dict):
                continue
            trades = resp.get("crypto_trades", [])
            for t in trades:
                sym   = t.get("symbol", "")
                conf  = t.get("confidence", 0)
                notional = t.get("notional_usdt", 0)
                entry = t.get("entry_target")
                tp    = t.get("tp_target")

                if sym not in CRYPTO_UNIVERSE:
                    # Allow coins discovered via market scan (has real volume on Binance.US)
                    scan_syms = {c["symbol"] for c in market_scan}
                    if sym not in scan_syms or not sym.endswith("USDT"):
                        self._log(f"   ⚠️ {sym} not in universe or market scan — skipping")
                        continue
                    self._log(f"   🌟 {sym} discovered via market scan — new opportunity!")
                if conf < CRYPTO_RULES["min_confidence"]:
                    continue
                if sym in self.positions:
                    continue
                if notional < CRYPTO_RULES["min_trade_usdt"]:
                    continue

                # Validate against projection
                proj = self._projections.get(sym, {})
                if proj.get("error") or not proj.get("viable"):
                    self._log(f"   ⚠️ {sym} projection not viable — skipping")
                    continue

                # Use ATR-based targets if available, else proj range
                atr       = proj.get("atr", 0)
                entry_px  = entry or proj["proj_low"]
                if atr and atr > 0:
                    tp_px  = tp or round(entry_px + 3.5 * atr, 6)
                    stop_px= round(entry_px - 1.5 * atr, 6)
                else:
                    tp_px  = tp or proj["proj_high"]
                    stop_px= round(entry_px * (1 - CRYPTO_RULES["stop_loss_pct"]), 6)

                # ── Fee-aware profit check ────────────────────
                # TP must be above entry + fees + min profit
                # Binance.US: 0.1% taker per trade = 0.2% round-trip
                fee_rt      = 0.002   # 0.2% round-trip
                min_profit  = 0.009   # 0.9% minimum net profit
                min_tp      = round(entry_px * (1 + fee_rt + min_profit), 8)
                if tp_px < min_tp:
                    tp_px = min_tp
                    self._log(f"   📐 {sym} TP floored to ${tp_px:.8f} (fee-aware minimum)")

                # Check projected gain is worth trading
                net_gain_pct = round((tp_px - entry_px) / entry_px * 100 - fee_rt * 100, 2)
                if net_gain_pct < 0.5:
                    self._log(f"   ⚠️ {sym} net gain only {net_gain_pct:.1f}% after fees — skipping")
                    continue

                proposals.append({
                    "symbol":    sym,
                    "notional":  min(notional, max(crypto_pool, total_sellable) * 0.6),
                    "entry":     entry_px,
                    "tp":        tp_px,
                    "stop":      stop_px,
                    "conf":      conf,
                    "owner":     ai_name,
                    "rationale": t.get("rationale", ""),
                })

        # Deduplicate — if both AIs agree on same coin, combine confidence
        seen = {}
        for p in proposals:
            sym = p["symbol"]
            if sym in seen:
                seen[sym]["conf"] = min(98, seen[sym]["conf"] + 10)
                seen[sym]["owner"] = "shared"
            else:
                seen[sym] = p

        final_proposals = sorted(seen.values(), key=lambda x: -x["conf"])

        # ── Execute: sell weak coins first if needed, then buy ──
        # If USDT is low but AI wants to buy, auto-sell the weakest
        # agreed coin first to fund the buy — no manual intervention needed
        for proposal in final_proposals[:CRYPTO_RULES["max_positions"] - len(self.positions)]:
            sym      = proposal["symbol"]
            notional = proposal["notional"]
            entry    = proposal["entry"]
            tp_price = proposal["tp"]
            owner    = proposal["owner"]
            conf     = proposal["conf"]

            # If not enough USDT — try to sell a weak coin first
            if crypto_pool < notional:
                needed    = notional - crypto_pool
                sold_usdt = 0.0

                # Find coins both AIs agree to sell (from sell_decisions above)
                agreed_sells = [s for s, d in sell_counts.items() if len(d) >= 2]

                # If no agreed sells, try single-AI sell for coins with bearish proj
                if not agreed_sells:
                    for ai_name, resp in [("claude", claude_resp), ("grok", grok_resp)]:
                        if not resp: continue
                        for sell in resp.get("sell_decisions", []):
                            ssym = sell.get("symbol", "")
                            if ssym and ssym not in agreed_sells:
                                proj = self._projections.get(ssym, {})
                                if proj.get("bias") in ("bearish", "neutral"):
                                    agreed_sells.append(ssym)

                for sell_sym in agreed_sells:
                    if sold_usdt >= needed:
                        break
                    asset   = sell_sym.replace("USDT", "")
                    holding = wallet_map.get(asset)
                    if not holding or holding.get("free", 0) <= 0:
                        continue
                    try:
                        # Try live price first, fall back to wallet stored price
                        curr_price = 0.0
                        try:
                            curr_price = get_crypto_price(sell_sym)
                        except Exception:
                            pass

                        # Fallback: use wallet's stored price (already read this cycle)
                        if curr_price <= 0:
                            curr_price = holding.get("price", 0)
                            if curr_price > 0:
                                self._log(f"   ℹ️ {sell_sym} using wallet price ${curr_price:.8f}")

                        qty = holding["free"]
                        val = qty * curr_price if curr_price > 0 else 0

                        # Skip dust
                        if curr_price > 0 and val < 2.0:
                            self._log(f"   ⚠️ {sell_sym} dust (${val:.4f}) — skipping sell")
                            continue

                        # Round qty to exchange step_size (prevents LOT_SIZE 400 errors)
                        qty = _round_qty_step(qty, sell_sym)
                        if qty <= 0:
                            self._log(f"   ⚠️ {sell_sym} rounded to 0 qty — skipping")
                            continue

                        if curr_price > 0:
                            # Verify live balance (stale cache guard — prevents 400 on already-sold coins)
                            try:
                                _live = binance_get("/api/v3/account", signed=True)
                                _free = float(next((b["free"] for b in _live["balances"]
                                                    if b["asset"] == holding["asset"]), "0"))
                                if _free < 0.01:
                                    self._log(f"   ℹ️ {sell_sym}: live={_free:.4f} — already sold, skipping")
                                    continue
                                qty = min(qty, _free)
                            except Exception:
                                pass  # fallback to cached qty
                            qty_r  = _round_qty_step(qty, sell_sym)
                            if qty_r <= 0:
                                self._log(f"   ⚠️ {sell_sym}: qty=0 after live check — skip")
                                continue
                            # MARKET sell for rotation — instant fill
                            result = binance_post("/api/v3/order", {
                                "symbol":    sell_sym,
                                "side":      "SELL",
                                "type":      "MARKET",
                                "quantity":  str(qty_r),
                                "timestamp": _timestamp(),
                            })
                        else:
                            # No price at all — use MARKET sell
                            self._log(f"   ⚠️ {sell_sym} no price — using MARKET sell")
                            result = binance_post("/api/v3/order", {
                                "symbol":      sell_sym,
                                "side":        "SELL",
                                "type":        "MARKET",
                                "quantity":    str(qty),
                                "timestamp":   _timestamp(),
                            })

                        if result.get("orderId"):
                            est_val     = val if val > 0 else 0
                            sold_usdt  += est_val
                            crypto_pool += est_val
                            self._log(f"   🔄 Sold {sell_sym} {qty} ~${est_val:.2f} → USDT "
                                      f"to fund {sym} buy | order={result['orderId']}")
                            import time as _t; _t.sleep(1.5)
                        else:
                            self._log(f"   ⚠️ Pre-sell failed for {sell_sym}: {result}")
                    except Exception as e:
                        self._log(f"   ⚠️ Pre-sell error {sell_sym}: {e}")

            if crypto_pool < notional:
                self._log(f"   💸 Still insufficient USDT (${crypto_pool:.2f}) "
                          f"for {sym} (${notional:.2f}) — skipping")
                continue

            try:
                self._log(f"   🟢 BUYING {sym} | ${notional:.2f} USDT | "
                          f"entry≤${entry} TP=${tp_price} | conf={conf}% [{owner}]")
                result = place_crypto_buy(sym, notional, entry)

                if result.get("orderId"):
                    qty = float(result.get("origQty", notional / max(entry, 0.000001)))
                    pos = CryptoPosition(
                        symbol      = sym,
                        qty         = qty,
                        entry_price = entry,
                        entry_time  = datetime.now(timezone.utc),
                        tp_price    = tp_price,
                        owner       = owner,
                    )
                    self.positions[sym] = pos
                    crypto_pool -= notional
                    new_positions += 1
                    # Mark as verified — price lookups will use this symbol directly
                    _VERIFIED_SYMBOLS.add(sym)
                    self._log(f"   ✅ Order placed: {result.get('orderId')} | "
                              f"stop=${pos.stop_price:.6f} TP=${pos.tp_price:.6f}")
                else:
                    self._log(f"   ❌ Order failed for {sym}: {result}")

            except Exception as e:
                self._log(f"   ❌ Buy error for {sym}: {e}")

        # ── Display AI strategy summary ────────────────────────
        self._log(f"   📋 CRYPTO STRATEGY SUMMARY (Cycle #{self.cycle_count}):")
        self._log(f"   Mode: {situation_mode.upper().replace('_',' ')} | "
                  f"USDT: ${crypto_pool:.2f} | Wallet: ${crypto_equity:.2f}")
        # Gains summary
        self.update_crypto_baselines(crypto_equity)
        gains_str = self.format_crypto_gains(crypto_equity)
        if gains_str:
            self._log(f"   {gains_str}")
        if grok_intel:
            self._log(f"   🌐 Grok live intel: {grok_intel[:180].strip()}")

        for ai_name, resp in [("Claude", claude_resp), ("Grok", grok_resp)]:
            if not resp or not isinstance(resp, dict):
                self._log(f"   {ai_name}: no response")
                continue
            note  = resp.get("market_note", "")
            avoid = resp.get("avoid", [])
            buys  = [t.get("symbol","") for t in resp.get("crypto_trades", [])
                     if t.get("action") == "buy"]
            holds = [h.get("symbol","") for h in resp.get("hold_decisions", [])]
            sells = [s.get("symbol","") for s in resp.get("sell_decisions", [])]
            self._log(f"   {ai_name}: "
                      + (f"BUY={buys} " if buys else "no buys ")
                      + (f"HOLD={holds} " if holds else "")
                      + (f"SELL={sells} " if sells else "")
                      + (f"AVOID={avoid} " if avoid else "")
                      + (f"| {note[:80]}" if note else ""))

        if not final_proposals:
            if not has_usdt and not has_coins:
                self._log(f"   💡 No trades: nothing to trade.")
            elif not has_usdt:
                self._log(f"   💡 No buys executed — waiting for USDT from coin sales or deposit.")
                self._log(f"      Tip: AI can sell coins to generate USDT if both AIs agree.")
            else:
                self._log(f"   💡 No trades: AIs found no high-confidence setups this cycle.")

        self._log_positions()
        self.last_cycle = datetime.now().isoformat()
        return new_positions

    def _log_positions(self):
        """Log current crypto positions."""
        if not self.positions:
            return
        for sym, pos in self.positions.items():
            try:
                current = get_crypto_price(sym)
                pnl     = pos.pnl_pct(current)
                icon    = "📈" if pnl >= 0 else "📉"
                self._log(f"   {icon} {sym}: entry=${pos.entry_price} "
                          f"now=${current:.4f} P&L={pnl:+.2f}% "
                          f"({pos.hours_held():.1f}h) | "
                          f"stop=${pos.stop_price} TP=${pos.tp_price}")
            except Exception:
                pass

    # ── STATUS & STATS ──────────────────────────────────────
    def get_status(self) -> dict:
        """Return full crypto status for /crypto_status API endpoint."""
        positions_data = {}
        for sym, pos in self.positions.items():
            try:
                current = get_crypto_price(sym)
                positions_data[sym] = {
                    **pos.to_dict(),
                    "current_price": current,
                    "pnl_pct":       pos.pnl_pct(current),
                    "pnl_usd":       round((current - pos.entry_price) * pos.qty, 2),
                }
            except Exception:
                positions_data[sym] = pos.to_dict()

        # Read live wallet
        try:
            wallet = get_full_wallet()
        except Exception:
            wallet = {"error": "wallet read failed", "usdt_free": 0, "total_value": 0}

        return {
            "enabled":        self._enabled,
            "cycle_count":    self.cycle_count,
            "wallet":         {
                "usdt_free":    wallet.get("usdt_free", 0),
                "total_value":  wallet.get("total_value", 0),
                "summary":      wallet.get("wallet_summary", ""),
                "tradeable":    wallet.get("tradeable", []),
                "non_tradeable": wallet.get("non_tradeable", []),
                "bnb":          wallet.get("bnb"),
            },
            "staking":        self.staking.get_staking_summary(),
            "bot_positions":  positions_data,
            "position_count": len(self.positions),
            "total_pnl":      round(self.total_pnl, 2),
            "wins":           self.wins,
            "losses":         self.losses,
            "win_rate":       round(self.wins / max(self.wins + self.losses, 1) * 100, 1),
            "last_cycle":     self.last_cycle,
            "last_projections": {
                k: {
                    "close":      v.get("close"),
                    "proj_high":  v.get("proj_high"),
                    "proj_low":   v.get("proj_low"),
                    "bias":       v.get("bias"),
                    "confidence": v.get("confidence"),
                    "viable":     v.get("viable"),
                }
                for k, v in self._projections.items()
                if not v.get("error")
            },
            "recent_trades": list(reversed(self.trade_history))[:10],
            "rules": {
                "stop_pct":       f"{CRYPTO_RULES['stop_loss_pct']*100:.0f}%",
                "tp_pct":         f"{CRYPTO_RULES['take_profit_pct']*100:.0f}%",
                "max_hold_hours": CRYPTO_RULES["max_hold_hours"],
                "maker_fee":      f"{CRYPTO_RULES['maker_fee']*100:.2f}%",
                "taker_fee":      f"{CRYPTO_RULES['taker_fee']*100:.2f}%",
                "pool_pct":       f"{CRYPTO_RULES['crypto_pool_pct']*100:.0f}%",
            }
        }

    # ── SNAPSHOT HELPERS (for unified R1 call) ──────────────
    # Called from collaborative_session() to build crypto section
    # of the R1 prompt — NO extra AI call needed.

    def get_projections_snapshot(self) -> str:
        """Formatted crypto projections for R1 prompt. Recomputes if empty."""
        if not self._enabled:
            return ""
        try:
            if not self._projections:
                self._projections = get_all_crypto_projections()
            return format_crypto_projections_for_ai(self._projections)
        except Exception as e:
            self._log(f"⚠️ Projection snapshot failed: {e}")
            return ""

    def get_wallet_snapshot(self) -> dict:
        """Wallet summary dict for R1 prompt."""
        if not self._enabled:
            return {"summary": "", "usdt_free": 0, "holdings_text": ""}
        try:
            wallet = get_full_wallet()
            holdings_lines = []
            for h in wallet.get("tradeable", []):
                proj       = self._projections.get(h["symbol"], {})
                note       = ""
                locked_qty = h.get("locked", 0)
                free_qty   = h.get("free", h["qty"])
                if proj and not proj.get("error"):
                    curr = h["price"]
                    ph   = proj.get("proj_high", 0)
                    pl   = proj.get("proj_low", 0)
                    if ph and pl:
                        if curr >= ph * 0.98:
                            note = " ⚠️ NEAR PROJ HIGH"
                        elif curr <= pl * 1.02:
                            note = " 🟢 AT PROJ LOW"
                        else:
                            note = f" → {round((ph-curr)/curr*100,1):.1f}% to TP"
                # Show free vs staked clearly — AI must only plan trades on free qty
                if locked_qty > 0.0001:
                    staked_note = f" | 🔒 {locked_qty:.4f} STAKED (cannot sell/trade)"
                    holdings_lines.append(
                        f"  {h['asset']}: {free_qty:.4f} FREE = ${round(free_qty * h['price'], 2):.2f}"
                        f" tradeable @ ${h['price']:.4f}{note}{staked_note}"
                    )
                else:
                    holdings_lines.append(
                        f"  {h['asset']}: {free_qty:.4f} = ${h['value_usdt']:.2f}"
                        f" @ ${h['price']:.4f}{note}"
                    )
            return {
                "summary":       wallet.get("wallet_summary", ""),
                "usdt_free":     wallet.get("usdt_free", 0),
                "total_usdt_value": wallet.get("total_value", wallet.get("usdt_free", 0)),
                "holdings_text": "\n".join(holdings_lines) if holdings_lines else "",
                "tradeable":     wallet.get("tradeable", []),
            }
        except Exception as e:
            self._log(f"⚠️ Wallet snapshot failed: {e}")
            return {"summary": "", "usdt_free": 0, "holdings_text": ""}

    def get_stats_snapshot(self) -> str:
        """Top 8 24h movers as compact string for R1 prompt."""
        if not self._enabled:
            return ""
        try:
            stats = get_all_crypto_stats()
            return str([(s["symbol"], f"{s['change_pct']:+.1f}%") for s in stats[:8]])
        except Exception:
            return ""

    def get_stock_cross_ref(self, stock_projections: dict) -> str:
        """BTC-correlated stock signals. BTC leads NVDA/AMD/MSTR/COIN by ~12h."""
        if not self._enabled or not stock_projections:
            return ""
        correlated = ["NVDA", "AMD", "MSTR", "COIN"]
        lines = []
        for sym in correlated:
            proj = stock_projections.get(sym, {})
            if proj and not proj.get("error"):
                bias = proj.get("bias", "neutral")
                conf = proj.get("confidence", 0)
                if conf >= 55:
                    lines.append(f"  {sym}: {bias.upper()} conf={conf}")
        if not lines:
            return ""
        return ("BTC-correlated stocks (BTC leads ~12h):\n" + "\n".join(lines))

    def execute_from_r1(self, claude_r1: dict, grok_r1: dict,
                        crypto_pool: float,
                        record_trade_fn=None,
                        prompt_builder=None) -> int:
        """
        Extract crypto_trades from R1 responses and execute them.
        Called from collaborative_session() — unified execution path.
        No extra AI call. Returns number of new positions opened.
        """
        if not self._enabled:
            return 0

        self.run_exit_monitor()

        if len(self.positions) >= CRYPTO_RULES["max_positions"]:
            return 0

        proposals = []
        for ai_name, resp in [("claude", claude_r1), ("grok", grok_r1)]:
            if not resp or not isinstance(resp, dict):
                continue
            for t in resp.get("crypto_trades", []):
                sym      = t.get("symbol", "")
                conf     = t.get("confidence", 0)
                notional = t.get("notional_usdt", 0)
                entry    = t.get("entry_target")
                tp       = t.get("tp_target")
                if sym not in CRYPTO_UNIVERSE:
                    continue
                if conf < CRYPTO_RULES["min_confidence"]:
                    continue
                if sym in self.positions:
                    continue
                if notional < CRYPTO_RULES["min_trade_usdt"]:
                    continue
                proj = self._projections.get(sym, {})
                if proj.get("error") or not proj.get("viable"):
                    self._log(f"   🪙 {sym} proj not viable — skip")
                    continue
                proposals.append({
                    "symbol":    sym,
                    "notional":  min(notional, crypto_pool * 0.6),
                    "entry":     entry or proj["proj_low"],
                    "tp":        tp    or proj["proj_high"],
                    "conf":      conf,
                    "owner":     ai_name,
                    "rationale": t.get("rationale", ""),
                })

        # Deduplicate — both agree = shared + confidence boost
        seen = {}
        for p in proposals:
            sym = p["symbol"]
            if sym in seen:
                seen[sym]["conf"]  = min(98, seen[sym]["conf"] + 10)
                seen[sym]["owner"] = "shared"
            else:
                seen[sym] = p

        final = sorted(seen.values(), key=lambda x: -x["conf"])
        new_positions = 0

        for prop in final[:CRYPTO_RULES["max_positions"] - len(self.positions)]:
            if crypto_pool < prop["notional"]:
                self._log(f"   🪙 Insufficient USDT for {prop['symbol']}")
                continue
            try:
                sym = prop["symbol"]
                self._log(f"   🪙 BUY {sym} | ${prop['notional']:.2f} USDT | "
                          f"entry≤${prop['entry']} TP=${prop['tp']} | "
                          f"conf={prop['conf']}% [{prop['owner']}]")
                result = place_crypto_buy(sym, prop["notional"], prop["entry"])
                if result.get("orderId"):
                    qty = float(result.get("origQty", prop["notional"] / prop["entry"]))
                    pos = CryptoPosition(
                        symbol      = sym,
                        qty         = qty,
                        entry_price = prop["entry"],
                        entry_time  = datetime.now(timezone.utc),
                        tp_price    = prop["tp"],
                        owner       = prop["owner"],
                    )
                    self.positions[sym] = pos
                    crypto_pool -= prop["notional"]
                    new_positions += 1
                    self._log(f"   ✅ Crypto order {result['orderId']} | "
                              f"stop=${pos.stop_price} TP=${pos.tp_price}")
                else:
                    self._log(f"   ❌ Crypto order failed: {result}")
            except Exception as e:
                self._log(f"   ❌ Crypto buy error {prop['symbol']}: {e}")

        return new_positions
