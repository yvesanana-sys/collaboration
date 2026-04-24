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
    # ── Stop / Profit targets (AGGRESSIVE SCALPING) ──────────
    # Strategy: take profits fast, small positions, compound many wins
    # Previous (too patient): 20% stop / 50% TP / 30% trail / 72h hold
    "stop_loss_pct":        0.08,    # -8% stop (was -20% — cuts losses fast)
    "take_profit_pct":      0.08,    # +8% TP (was +50% — banks wins fast)
    "trail_activate_pct":   0.03,    # Start trailing at +3% (was +30%)
    "trail_pct":            0.025,   # Tight 2.5% trail (was 40% — lock gains fast)
    "min_profit_pct":       0.03,    # 3% minimum expected (was 5%)
    "max_hold_hours":       24,      # 1-day time stop (was 72h)
    # ── Position sizing (tier-based) ─────────────────────────
    # More aggressive at small equity — needed to compound to goal
    "max_positions":        3,       # Aggressive — 3 concurrent crypto positions (was 2)
    "min_trade_usdt":       8.0,     # Binance.US minimum
    # ── Entry filters ─────────────────────────────────────────
    "min_confidence":       60,      # Aggressive — lowered to 60 (was 65)
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
    # AGGRESSIVE PROFILE + DISCOVERY MODE ENABLED
    # coins list is ADVISORY (prefer these) — AI can buy anything from scan
    # if confidence ≥ 70%. No hard tier restrictions on buys.
    {"min_equity":   0, "max_equity": 150,  "risk_pct": 0.30, "max_pos": 3,
     "coins": None,  # DISCOVERY MODE — AI can pick from top market movers
     "note": "Tier 1 — AGGRESSIVE + DISCOVERY: 30% risk, 3 positions, any trending coin"},
    {"min_equity": 150, "max_equity": 300,  "risk_pct": 0.25, "max_pos": 3,
     "coins": None,
     "note": "Tier 2 — 25% risk, 3 positions, full discovery"},
    {"min_equity": 300, "max_equity": 600,  "risk_pct": 0.20, "max_pos": 4,
     "coins": None,
     "note": "Tier 3 — 20% risk, 4 positions, full discovery"},
    {"min_equity": 600, "max_equity": 9999, "risk_pct": 0.15, "max_pos": 5,
     "coins": None,
     "note": "Tier 4 — 15% risk, 5 positions, full discovery"},
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
    """
    Get 24h volume and stats for a crypto symbol.
    Returns dict with: volume, quoteAssetVolume, priceChange, priceChangePercent, lastPrice
    """
    return binance_get("/api/v3/ticker/24hr", {"symbol": symbol})


# ══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════

def get_account_balance() -> dict:
    """
    Get full account balance from Binance.US.
    Returns: {total_balance_usdt, assets: {symbol: qty, ...}}
    """
    data = binance_get("/api/v3/account", signed=True)
    assets = {}
    for bal in data.get("balances", []):
        qty = float(bal["free"]) + float(bal["locked"])
        if qty > 0:
            assets[bal["asset"]] = qty
    
    # Calculate total in USDT
    total_usdt = assets.get("USDT", 0)
    for asset, qty in assets.items():
        if asset != "USDT":
            try:
                price = get_crypto_price(f"{asset}USDT")
                total_usdt += qty * price
            except Exception:
                pass
    
    return {"total_balance_usdt": total_usdt, "assets": assets}

def get_open_orders(symbol: str = None) -> list:
    """
    Get all open orders, optionally filtered by symbol.
    Returns list of order dicts.
    """
    params = {}
    if symbol:
        params["symbol"] = symbol
    return binance_get("/api/v3/openOrders", params, signed=True)

def place_limit_order(symbol: str, side: str, quantity: float, price: float) -> dict:
    """
    Place a limit order (maker = 0% fee).
    side: 'BUY' or 'SELL'
    Returns order confirmation dict.
    """
    return binance_post("/api/v3/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "quantity": quantity,
        "price": price,
        "timeInForce": "GTC",
    })

def cancel_order(symbol: str, order_id: int) -> dict:
    """
    Cancel an open order.
    """
    return binance_delete("/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    })

def get_order_status(symbol: str, order_id: int) -> dict:
    """
    Get status of a specific order.
    """
    return binance_get("/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    }, signed=True)


# ══════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════

def calculate_rsi(closes: list, period: int = 14) -> float:
    """
    Calculate RSI (Relative Strength Index).
    Returns value 0-100.
    """
    if len(closes) < period + 1:
        return 50.0
    
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_sma(closes: list, period: int) -> float:
    """
    Calculate Simple Moving Average.
    """
    if len(closes) < period:
        return closes[-1] if closes else 0
    return sum(closes[-period:]) / period

def calculate_atr(bars: list, period: int = 14) -> float:
    """
    Calculate Average True Range.
    bars should be list of {h, l, c} dicts.
    """
    if len(bars) < period:
        return 0
    
    tr_list = []
    for i in range(1, len(bars)):
        h = bars[i]["h"]
        l = bars[i]["l"]
        c_prev = bars[i-1]["c"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    
    return sum(tr_list[-period:]) / period


# ══════════════════════════════════════════════════════════════
# TRADING LOGIC
# ══════════════════════════════════════════════════════════════

class CryptoTrader:
    """
    Main crypto trading engine.
    Runs 24/7 via bot_with_proxy.py trading_loop().
    """
    
    def __init__(self):
        self.positions = {}  # {symbol: {qty, entry_price, stop, tp, trail_stop, entry_time}}
        self.order_cache = {}  # {symbol: order_id}
    
    def run_crypto_cycle(self, wallet_equity: float) -> dict:
        """
        Main entry point from bot_with_proxy.py.
        Runs every minute (or on AI wake) to:
        1. Update open positions (exits)
        2. Scan for entry opportunities
        3. Place new orders
        Returns summary dict.
        """
        try:
            # Get current balance
            bal = get_account_balance()
            total_usdt = bal["total_balance_usdt"]
            
            # Update positions (exits, trailing stops)
            self._update_positions(bal, total_usdt)
            
            # Scan for entries if we have cash and slots
            if total_usdt > CRYPTO_RULES["min_trade_usdt"]:
                self._scan_and_enter(bal, total_usdt)
            
            return {
                "status": "ok",
                "wallet_total_usdt": total_usdt,
                "open_positions": len(self.positions),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
    
    def _update_positions(self, balance: dict, total_usdt: float) -> None:
        """
        Check all open positions for:
        - Stop loss hits
        - Take profit hits
        - Trailing stop adjustments
        - Timeout exits (72h max hold)
        """
        # TODO: implement position updates
        pass
    
    def _scan_and_enter(self, balance: dict, total_usdt: float) -> None:
        """
        Scan top movers for entry opportunities.
        Uses technical analysis + AI signals.
        TODO: AI will expand this with ML scoring.
        """
        # TODO: implement entry scanning
        pass


# ══════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════

def round_to_decimals(value: float, decimals: int) -> float:
    """
    Round a value to N decimal places for Binance lot size.
    """
    if decimals == 0:
        return float(int(value))
    return round(value, decimals)

def format_price(price: float, symbol: str) -> float:
    """
    Format price for a symbol per Binance tick precision.
    """
    decimals = CRYPTO_UNIVERSE.get(symbol, {}).get("decimals", 2)
    return round_to_decimals(price, decimals)

def is_within_notional_limits(symbol: str, quantity: float, price: float) -> bool:
    """
    Check if order meets Binance minimum notional value.
    """
    notional = quantity * price
    min_notional = CRYPTO_UNIVERSE.get(symbol, {}).get("min_notional", 10.0)
    return notional >= min_notional
