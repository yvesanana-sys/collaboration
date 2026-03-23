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
    # ── Major coins ───────────────────────────────────────────
    "BTCUSDT":   {"name": "Bitcoin",    "min_notional": 10.0, "decimals": 5},
    "ETHUSDT":   {"name": "Ethereum",   "min_notional": 10.0, "decimals": 4},
    "SOLUSDT":   {"name": "Solana",     "min_notional": 10.0, "decimals": 3},
    "AVAXUSDT":  {"name": "Avalanche",  "min_notional": 10.0, "decimals": 3},
    "DOGEUSDT":  {"name": "Dogecoin",   "min_notional": 10.0, "decimals": 0},
    "LINKUSDT":  {"name": "Chainlink",  "min_notional": 10.0, "decimals": 3},
    "ADAUSDT":   {"name": "Cardano",    "min_notional": 10.0, "decimals": 1},
    "DOTUSDT":   {"name": "Polkadot",   "min_notional": 10.0, "decimals": 3},
    # ── Your current holdings — always tracked + tradeable ────
    "FETUSDT":   {"name": "Fetch.ai",   "min_notional": 10.0, "decimals": 1},
    "SHIBUSDT":  {"name": "Shiba Inu",  "min_notional": 10.0, "decimals": 0},
    "AUDIOUSDT": {"name": "Audius",     "min_notional": 10.0, "decimals": 1},
    "KAVAUSDT":  {"name": "Kava",       "min_notional": 10.0, "decimals": 3},
    "RVNUSDT":   {"name": "Ravencoin",  "min_notional": 10.0, "decimals": 0},
}

# ── Crypto Trading Rules ──────────────────────────────────────
CRYPTO_RULES = {
    "stop_loss_pct":        0.04,    # -4% stop (crypto moves fast)
    "take_profit_pct":      0.05,    # 5% TP minimum (covers fees + profit)
    "min_profit_pct":       0.025,   # 2.5% minimum to enter (fee-aware)
    "max_hold_hours":       72,      # Hard exit after 72 hours
    "max_positions":        2,       # Max 2 crypto positions at once
    "min_confidence":       70,      # AI confidence threshold
    "maker_fee":            0.0000,  # 0% maker fee (Binance.US Tier 0)
    "taker_fee":            0.0001,  # 0.01% taker fee
    "round_trip_fee":       0.0002,  # 0.02% total round trip
    "crypto_pool_pct":      0.30,    # 30% of equity reserved for crypto
    "min_trade_usdt":       10.0,    # Minimum trade size $10
    "proj_conf_threshold":  65,      # Projection confidence minimum
    "proj_min_range_pct":   0.025,   # Projection must show 2.5%+ range
}


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

def get_crypto_price(symbol: str) -> float:
    """Get current price for a crypto symbol."""
    data = binance_get("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

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

    return {
        "close":    round(close, 4),
        "rsi":      rsi_val,
        "macd":     macd_val,
        "sma20":    round(sma20, 4) if sma20 else None,
        "sma50":    round(sma50, 4) if sma50 else None,
        "ema9":     round(ema9, 4) if ema9 else None,
        "ema21":    round(ema21, 4) if ema21 else None,
        "bb_pct":   bb_pct,
        "vol_ratio": vol_ratio,
        "atr":      atr_val,
        "mom_24h":  mom_24h,
        "mom_7d":   mom_7d,
    }


# ══════════════════════════════════════════════════════════════
# CRYPTO PROJECTION ENGINE
# Adapted from stock projection_engine.py for crypto volatility
# ══════════════════════════════════════════════════════════════

def get_crypto_projection(symbol: str, bars: list, ind: dict) -> dict:
    """
    5-layer daily range projection adapted for crypto.
    Key differences from stock version:
    - Uses hourly bars instead of daily
    - ATR multipliers are higher (crypto is more volatile)
    - Confidence calibrated for 24/7 markets
    - Min range check ensures 2.5%+ for fee viability
    """
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
    """Format crypto projections as actionable AI prompt text."""
    lines = ["CRYPTO PROJECTIONS (24h range, hourly data):"]
    for sym, proj in sorted(projections.items(),
                            key=lambda x: x[1].get("confidence", 0), reverse=True):
        if proj.get("error"):
            continue
        viable_tag = "✅ VIABLE" if proj.get("viable") else "⚠️ TIGHT"
        conf_tag   = "HIGH" if proj["confidence"] >= 70 else "MED" if proj["confidence"] >= 50 else "LOW"
        lines.append(
            f"  {sym}: ${proj['close']} | range ${proj['proj_low']}–${proj['proj_high']} "
            f"({proj['range_pct']:.1f}%) | {proj['bias'].upper()} | "
            f"conf={proj['confidence']} {conf_tag} | RSI={proj['rsi']} | {viable_tag}"
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

def get_staking_info(asset: str = None) -> list:
    """
    Get staking balance, APY, rewards, and auto-restake status.
    If asset is None, returns info for all staked assets.

    Returns list of dicts:
    {
      "asset":           str,    # e.g. "SOL"
      "staked_qty":      float,  # Amount currently staked
      "staked_value":    float,  # Value in USDT
      "apy":             float,  # Annual percentage yield
      "rewards_pending": float,  # Pending rewards not yet distributed
      "auto_restake":    bool,   # Is auto-restake on?
      "unbonding_days":  int,    # Days to unstake
      "annual_yield_usdt": float, # Expected yearly $ return
      "weekly_yield_usdt": float, # Expected weekly $ return
    }
    """
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
    """
    Read the COMPLETE Binance.US wallet — every coin, every balance.
    Not just our preset universe — everything you hold.

    Returns:
      {
        "usdt_free":      float,   # Spendable USDT
        "usdt_total":     float,   # Total USDT including locked
        "total_value":    float,   # Total wallet value in USDT
        "positions":      [...],   # All non-zero coin holdings
        "stablecoins":    [...],   # USDT, BUSD, USDC holdings
        "tradeable":      [...],   # Coins in our universe (can trade)
        "non_tradeable":  [...],   # Coins NOT in our universe (hold only)
        "wallet_summary": str,     # Human-readable summary for AI prompt
      }
    """
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
                # Coin not tradeable against USDT on Binance.US
                non_tradeable.append({
                    "asset":       asset,
                    "symbol":      symbol,
                    "qty":         qty,
                    "value_usdt":  0,
                    "in_universe": False,
                    "note":        "no USDT pair",
                })

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
                    lines.append(f"    {p['asset']}: {p['qty']:.4f} = ${p.get('value_usdt', 0):.2f}")
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

def place_crypto_buy(symbol: str, notional_usdt: float,
                     limit_price: float = None) -> dict:
    """
    Place a limit buy order.
    Uses proj_low as limit price if not specified — ensures 0% maker fee.
    notional_usdt: how much USDT to spend
    """
    if not limit_price:
        limit_price = get_crypto_price(symbol) * 0.9985  # 0.15% below market

    qty = _round_qty(notional_usdt / limit_price, symbol)
    price_str = f"{limit_price:.4f}"

    # Minimum notional check
    if notional_usdt < CRYPTO_RULES["min_trade_usdt"]:
        return {"error": f"Notional ${notional_usdt:.2f} below minimum $10"}

    return binance_post("/api/v3/order", {
        "symbol":      symbol,
        "side":        "BUY",
        "type":        "LIMIT",
        "timeInForce": "GTC",
        "quantity":    qty,
        "price":       price_str,
    })

def place_crypto_sell(symbol: str, qty: float,
                      limit_price: float = None) -> dict:
    """
    Place a limit sell order.
    Uses proj_high as limit price if not specified.
    """
    if not limit_price:
        limit_price = get_crypto_price(symbol) * 1.0015  # 0.15% above market

    qty        = _round_qty(qty, symbol)
    price_str  = f"{limit_price:.4f}"

    return binance_post("/api/v3/order", {
        "symbol":      symbol,
        "side":        "SELL",
        "type":        "LIMIT",
        "timeInForce": "GTC",
        "quantity":    qty,
        "price":       price_str,
    })

def place_crypto_stop_market(symbol: str, qty: float,
                              stop_price: float) -> dict:
    """
    Place a stop-loss market order for emergency exits.
    Used only when price hits stop — uses market order (0.01% taker fee).
    """
    qty = _round_qty(qty, symbol)
    return binance_post("/api/v3/order", {
        "symbol":    symbol,
        "side":      "SELL",
        "type":      "STOP_LOSS",
        "timeInForce": "GTC",
        "quantity":  qty,
        "stopPrice": f"{stop_price:.4f}",
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
        self.staking        = StakingManager()   # Staking manager

        if self._enabled:
            self._log("🔐 Binance.US API keys found — crypto trading ENABLED")
            self._log("🔒 Staking manager initialized — reviews every 12 hours")
        else:
            self._log("⚠️ BINANCE_KEY or BINANCE_SECRET missing — crypto trading DISABLED")
            self._log("   Add BINANCE_KEY and BINANCE_SECRET to Railway variables")

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
        Check all open positions for stop/TP/time exit.
        Runs every cycle — no AI needed.
        Returns number of exits executed.
        """
        if not self._enabled or not self.positions:
            return 0

        exits = 0
        for symbol, pos in list(self.positions.items()):
            try:
                current = get_crypto_price(symbol)
                pos.update(current)
                pnl = pos.pnl_pct(current)

                exit_reason = None
                if pos.should_stop(current):
                    exit_reason = f"stop_loss ({pnl:.2f}%)"
                elif pos.should_take_profit(current):
                    exit_reason = f"take_profit ({pnl:.2f}%)"
                elif pos.should_time_exit():
                    exit_reason = f"time_exit ({pos.hours_held():.1f}h > 72h)"

                if exit_reason:
                    result = self._execute_exit(pos, current, exit_reason)
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
        """
        Full AI collaboration cycle for crypto.
        Now fully integrated with:
          - prompt_builder (situation classification + learned lessons)
          - Capitol Trades politician signals
          - Smart money / triple confirmation
          - Stock projection engine cross-reference
          - record_trade() feeds main trade history + prompt memory
        """
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
        try:
            wallet        = get_full_wallet()
            crypto_pool   = wallet["usdt_free"]
            wallet_text   = wallet["wallet_summary"]
            tradeable     = wallet["tradeable"]
            # Use Binance wallet total as crypto equity — independent of Alpaca
            crypto_equity = wallet.get("total_value", max(total_equity * 0.3, crypto_pool))

            # ── Always log what we found ──────────────────────
            if wallet.get("error"):
                self._log(f"   ⚠️ Wallet error: {wallet['error']}")
            else:
                self._log(f"   💵 USDT free: ${crypto_pool:.2f} | "
                          f"Total wallet: ${crypto_equity:.2f}")
                if tradeable:
                    for h in tradeable[:4]:
                        self._log(f"   🪙 {h['asset']}: {h['qty']:.4f} "
                                  f"= ${h['value_usdt']:.2f} @ ${h['price']:.4f}")
                elif crypto_pool < CRYPTO_RULES["min_trade_usdt"]:
                    self._log(f"   ℹ️  No crypto holdings, USDT=${crypto_pool:.2f} "
                              f"(min ${CRYPTO_RULES['min_trade_usdt']})")
                if wallet.get("bnb"):
                    bnb = wallet["bnb"]
                    self._log(f"   🔶 BNB: {bnb['qty']:.4f} = ${bnb['value_usdt']:.2f}")
                staked = [s for s in wallet.get("non_tradeable", [])
                          if s.get("value_usdt", 0) > 1]
                if staked:
                    staked_summary = [(s["asset"], f'${s["value_usdt"]:.2f}') for s in staked[:3]]
                    self._log(f"   📦 Other holdings: {staked_summary}")
        except Exception as e:
            self._log(f"   ⚠️ Wallet read failed: {e}")
            return 0

        if crypto_pool < CRYPTO_RULES["min_trade_usdt"] and not tradeable:
            self._log(f"   Insufficient USDT (${crypto_pool:.2f}) and no holdings — skipping")
            return 0

        # Get market data + crypto projections
        self._log("   📊 Computing crypto projections...")
        try:
            self._projections = get_all_crypto_projections()
            stats             = get_all_crypto_stats()
        except Exception as e:
            self._log(f"   ⚠️ Market data failed: {e}")
            return 0

        proj_text  = format_crypto_projections_for_ai(self._projections)
        stats_text = [(s["symbol"], f"{s['change_pct']:+.2f}%",
                       f"vol={s['quote_volume']/1e6:.1f}M")
                      for s in stats[:8]]

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
            if near_stop:
                situation_mode = "defensive"
                focus = f"Crypto positions near stop: {near_stop}. Protect capital."
            elif crypto_pnl_pct <= -0.05:
                # Only damage control if CRYPTO P&L is bad — not stock losses
                situation_mode = "damage_control"
                focus = f"Crypto P&L {crypto_pnl_pct*100:.1f}%. Review positions."
            elif near_tp:
                situation_mode = "harvest_profits"
                focus = f"Crypto positions near TP: {near_tp}. Lock in gains."
            elif crypto_pool >= CRYPTO_RULES["min_trade_usdt"] * 2:
                # Have enough USDT — look for opportunities including dips
                situation_mode = "opportunity_seeking"
                focus = "USDT available. Seek 2-3 day momentum setups. Dips are entries."
            elif self.positions:
                situation_mode = "standard_monitoring"
                focus = "Managing open crypto positions."
            else:
                situation_mode = "capital_conservation"
                focus = "Low USDT. Monitor only."

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

        # ── Summarize existing holdings ───────────────────────
        existing = [(s, p.pnl_pct(get_crypto_price(s)))
                    for s, p in self.positions.items()] if self.positions else []
        win_rate = round(self.wins / max(self.wins + self.losses, 1) * 100, 0)

        holdings_text = ""
        if tradeable:
            holdings_text = "\nEXISTING HOLDINGS (decide: hold/add/sell):\n"
            for h in tradeable:
                proj = self._projections.get(h["symbol"], {})
                proj_note = ""
                if proj and not proj.get("error"):
                    curr = h["price"]
                    ph   = proj.get("proj_high", 0)
                    pl   = proj.get("proj_low", 0)
                    if ph and pl:
                        if curr >= ph * 0.98:
                            proj_note = f" ⚠️ NEAR PROJ HIGH ${ph} — consider selling"
                        elif curr <= pl * 1.02:
                            proj_note = f" 🟢 AT PROJ LOW ${pl} — good add zone"
                        else:
                            dist_tp = round((ph - curr) / curr * 100, 1)
                            proj_note = f" → {dist_tp:.1f}% to proj_high ${ph}"
                holdings_text += (f"  {h['asset']}: {h['qty']:.4f} "
                                  f"= ${h['value_usdt']:.2f} @ ${h['price']:.4f}"
                                  f"{proj_note}\n")

        # ── Build situation-aware system prompts ──────────────
        if prompt_builder:
            claude_system = prompt_builder.build_claude_system()
            grok_system   = prompt_builder.build_grok_system()
            # Append crypto context to system prompts
            claude_system = claude_system.replace(
                "ONLY valid JSON under 500 chars.",
                "Focus on crypto 2-3 day momentum. ONLY valid JSON under 500 chars."
            )
            grok_system = grok_system.replace(
                "ONLY valid JSON under 500 chars.",
                "Use Twitter/X crypto sentiment. ONLY valid JSON under 500 chars."
            )
        else:
            claude_system = ("You are Claude analyzing crypto for NovaTrade. "
                             "ONLY valid JSON under 500 chars.")
            grok_system   = ("You are Grok analyzing crypto with Twitter/X "
                             "sentiment access. ONLY valid JSON under 500 chars.")

        # ── Assemble full prompt ──────────────────────────────
        prompt = f"""=== CRYPTO TRADING — NOVATRADE 24/7 [{situation_mode.upper().replace('_',' ')}] ===
{wallet_text}

Available USDT: ${crypto_pool:.2f} | Bot positions: {existing or 'None'}
Crypto P&L: ${self.total_pnl:+.2f} | Win rate: {int(win_rate)}% ({self.wins}W/{self.losses}L)
SPY trend: {spy_trend.upper()} (context only — crypto trades independently)
Binance.US wallet total: ${crypto_equity:.2f}
{holdings_text}
CRYPTO RULES:
- Min profit: {CRYPTO_RULES['min_profit_pct']*100:.1f}% | Stop: {CRYPTO_RULES['stop_loss_pct']*100:.0f}% | Max hold: {CRYPTO_RULES['max_hold_hours']}h
- Always LIMIT orders (0% maker fee) | Entry ≤ proj_low | Exit at proj_high
- Only VIABLE projections (range > 2.5%)
- CRYPTO IS INDEPENDENT: stock losses, SPY weakness, low Alpaca cash do NOT affect crypto decisions
- DIPS ARE ENTRIES: bearish SPY or red stock day = potential crypto buying opportunity (oversold bounce)
- Trade 24/7: weekend, afterhours, overnight — all valid if setup is right

24H MOVERS: {stats_text}

{proj_text}

{stock_cross_ref}

{pol_section}

{smart_section}

{f"LEARNED CONTEXT (from past trades):{chr(10)}{lessons_text}" if lessons_text else ""}

TASK:
1. HOLDINGS: hold / add / sell each coin you own (use proj_high/low)
2. NEW BUYS: 1-2 best 2-3 day setups — bullish momentum OR oversold dip near proj_low (RSI<45)
3. DIPS: market weakness = entry opportunity if proj shows recovery potential
4. AVOID: coins with bearish proj AND no recovery signal

JSON: {{"crypto_trades":[{{"symbol":"BTCUSDT","action":"buy","notional_usdt":12.0,"confidence":80,"entry_target":95000.0,"tp_target":97500.0,"rationale":"brief","owner":"claude"}}],"hold_decisions":[{{"symbol":"ETHUSDT","action":"hold","reason":"brief"}}],"sell_decisions":[{{"symbol":"SOLUSDT","action":"sell","reason":"near proj_high"}}],"avoid":["DOGEUSDT"],"market_note":"brief"}}"""

        # ── Ask both AIs ──────────────────────────────────────
        self._log("   🔵 Claude analyzing crypto...")
        self._log("   🔴 Grok analyzing crypto...")

        claude_resp = None
        grok_resp   = None

        try:
            claude_resp = ask_claude_fn(prompt, claude_system)
        except Exception as e:
            self._log(f"   ⚠️ Claude crypto failed: {e}")

        try:
            grok_resp = ask_grok_fn(prompt, grok_system)
        except Exception as e:
            self._log(f"   ⚠️ Grok crypto failed: {e}")

        if not claude_resp and not grok_resp:
            self._log("   ⚠️ Both AIs failed — skipping crypto cycle")
            return 0

        # ── Process SELL decisions on existing holdings ───────
        sell_decisions = []
        for ai_name, resp in [("claude", claude_resp), ("grok", grok_resp)]:
            if not resp or not isinstance(resp, dict):
                continue
            for sell in resp.get("sell_decisions", []):
                sym = sell.get("symbol", "")
                if sym:
                    sell_decisions.append((sym, sell.get("reason", "AI recommendation"), ai_name))

        # Execute sells both AIs agree on (or single AI if holding is near proj_high)
        sell_counts = {}
        for sym, reason, ai in sell_decisions:
            sell_counts[sym] = sell_counts.get(sym, [])
            sell_counts[sym].append((reason, ai))

        for sym, decisions in sell_counts.items():
            # Both AIs agree → sell
            # Or single AI says sell AND price is within 1% of proj_high
            both_agree = len(decisions) >= 2
            proj = self._projections.get(sym, {})
            near_proj_high = False
            if proj and not proj.get("error"):
                try:
                    curr = get_crypto_price(sym)
                    ph   = proj.get("proj_high", 0)
                    if ph and curr >= ph * 0.99:
                        near_proj_high = True
                except Exception:
                    pass

            if both_agree or near_proj_high:
                reason = decisions[0][0]
                self._log(f"   🔴 AI sell signal: {sym} — {reason} "
                          f"({'both agreed' if both_agree else 'near proj_high'})")
                try:
                    # Find qty from wallet
                    asset = sym.replace("USDT", "")
                    wallet_pos = next((p for p in tradeable if p["asset"] == asset), None)
                    if wallet_pos:
                        curr  = get_crypto_price(sym)
                        result = place_crypto_sell(sym, wallet_pos["qty"],
                                                   round(curr * 1.001, 4))
                        if result.get("orderId"):
                            self._log(f"   ✅ Sell order placed for {sym}: {result['orderId']}")
                        else:
                            self._log(f"   ⚠️ Sell order failed: {result}")
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
                    continue
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

                proposals.append({
                    "symbol":   sym,
                    "notional": min(notional, crypto_pool * 0.6),
                    "entry":    entry or proj["proj_low"],
                    "tp":       tp or proj["proj_high"],
                    "conf":     conf,
                    "owner":    ai_name,
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

        # Execute best proposals
        for proposal in final_proposals[:CRYPTO_RULES["max_positions"] - len(self.positions)]:
            if crypto_pool < proposal["notional"]:
                self._log(f"   💸 Insufficient USDT for {proposal['symbol']}")
                continue

            sym      = proposal["symbol"]
            notional = proposal["notional"]
            entry    = proposal["entry"]
            tp_price = proposal["tp"]
            owner    = proposal["owner"]
            conf     = proposal["conf"]

            try:
                self._log(f"   🟢 BUYING {sym} | ${notional:.2f} USDT | "
                          f"entry≤${entry} TP=${tp_price} | conf={conf}% [{owner}]")
                result = place_crypto_buy(sym, notional, entry)

                if result.get("orderId"):
                    qty = float(result.get("origQty", notional / entry))
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
                    self._log(f"   ✅ Order placed: {result.get('orderId')} | "
                              f"stop=${pos.stop_price} TP=${pos.tp_price}")
                else:
                    self._log(f"   ❌ Order failed for {sym}: {result}")

            except Exception as e:
                self._log(f"   ❌ Buy error for {sym}: {e}")

        # ── Display AI strategy summary ────────────────────────
        self._log(f"   📋 CRYPTO STRATEGY SUMMARY (Cycle #{self.cycle_count}):")
        self._log(f"   Mode: {situation_mode.upper().replace('_',' ')} | "
                  f"USDT: ${crypto_pool:.2f} | Wallet: ${crypto_equity:.2f}")

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
            if crypto_pool < CRYPTO_RULES["min_trade_usdt"]:
                self._log(f"   💡 No trades: insufficient USDT (${crypto_pool:.2f}). "
                          f"Convert holdings to USDT to enable buying.")
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
                proj = self._projections.get(h["symbol"], {})
                note = ""
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
                holdings_lines.append(
                    f"  {h['asset']}: {h['qty']:.4f} = ${h['value_usdt']:.2f}"
                    f" @ ${h['price']:.4f}{note}"
                )
            return {
                "summary":       wallet.get("wallet_summary", ""),
                "usdt_free":     wallet.get("usdt_free", 0),
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
