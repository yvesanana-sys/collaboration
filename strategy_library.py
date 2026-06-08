"""
strategy_library.py  —  NovaTrade modular Strategy Library (Batch 1)
====================================================================

A SIGNAL-ONLY library. Nothing in here talks to Alpaca or Binance and nothing
places an order. Each strategy consumes an OHLCV DataFrame and emits a single
structured `Signal` (or None). An overarching "Committee" layer (separate module,
not in this batch) is responsible for collecting these signals, weighting them by
market regime, and deciding what — if anything — to execute.

Why this is its own file:
  - It keeps the 4,800-line bot_with_proxy.py untouched (and out of the
    >100KB rewrite-truncation blast radius).
  - It can be unit-tested and shadow-tested in isolation before a single
    real dollar is sized.

Design contract every strategy obeys:
  - evaluate(df, symbol, equity, capital_available) -> Optional[Signal]
  - Decision is made ONLY on the most recent CLOSED bar (df.iloc[-1]).
  - Stop is dynamic (swing-based and/or ATR-based).
  - Take-profit is a strict 1:2 reward:risk multiple of the stop distance.
  - Position size risks at most `risk_pct` (default 1%) of total equity.

Dependencies: pandas, numpy. (ccxt only for the optional live-data demo.)
Indicators are implemented natively to avoid the pandas_ta / NumPy 2.x breakage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Signal container
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    """A normalized trade idea. The Committee, not the strategy, decides to act."""
    strategy: str
    symbol: str
    direction: str          # "long" | "short"
    entry: float            # reference entry price (last close)
    stop: float             # dynamic stop-loss price
    take_profit: float      # strict 1:2 R:R target
    size: float             # position size in UNITS of the asset
    notional: float         # size * entry, in quote currency
    risk_amount: float      # quote-currency $ at risk if stop is hit (~1% equity)
    rr: float               # realized reward:risk of the levels (should be ~2.0)
    confidence: float       # 0-100, strategy's own conviction
    reason: str             # human-readable rationale
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


# ---------------------------------------------------------------------------
# Indicator primitives (native pandas/numpy — no pandas_ta)
# ---------------------------------------------------------------------------
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing == EMA with alpha = 1/length
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)  # neutral when undefined (flat series)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).fillna(0.0).cumsum()


def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0):
    mid = sma(close, length)
    sd = close.rolling(length).std(ddof=0)
    upper = mid + mult * sd
    lower = mid - mult * sd
    return lower, mid, upper


def keltner(df: pd.DataFrame, length: int = 20, mult: float = 1.5):
    mid = ema(df["close"], length)
    rng = atr(df, length)
    upper = mid + mult * rng
    lower = mid - mult * rng
    return lower, mid, upper


def rolling_vwap(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Rolling (windowed) VWAP — works without session boundaries on 24/7 crypto."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).rolling(length).sum()
    vol = df["volume"].rolling(length).sum()
    return pv / vol


def recent_swing_low(df: pd.DataFrame, lookback: int = 10) -> float:
    """Lowest low over the prior `lookback` bars (excludes the current bar)."""
    return float(df["low"].iloc[-(lookback + 1):-1].min())


def recent_swing_high(df: pd.DataFrame, lookback: int = 10) -> float:
    return float(df["high"].iloc[-(lookback + 1):-1].max())


# ---------------------------------------------------------------------------
# Risk / sizing helpers  (shared by every strategy — single source of truth)
# ---------------------------------------------------------------------------
def position_size(equity: float, entry: float, stop: float,
                  risk_pct: float = 0.01,
                  capital_available: Optional[float] = None) -> tuple[float, float, float]:
    """
    Size so that hitting the stop loses at most `risk_pct` of equity.

    Returns (units, notional, risk_amount).
    units = 0 means "do not trade" (stop too close / invalid).
    """
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0 or entry <= 0:
        return 0.0, 0.0, 0.0

    risk_amount = equity * risk_pct
    units = risk_amount / risk_per_unit
    notional = units * entry

    # Never deploy more cash than is available (1% risk can still imply a large
    # notional when the stop is very tight — clamp it).
    if capital_available is not None and notional > capital_available:
        units = capital_available / entry
        notional = units * entry
        risk_amount = units * risk_per_unit  # actual $ risked after the clamp

    return units, notional, risk_amount


def take_profit_1to2(entry: float, stop: float, direction: str) -> float:
    """Strict 1:2 reward:risk target."""
    r = abs(entry - stop)
    return entry + 2 * r if direction == "long" else entry - 2 * r


def build_signal(strategy: str, symbol: str, direction: str,
                 entry: float, stop: float, equity: float,
                 confidence: float, reason: str,
                 risk_pct: float = 0.01,
                 capital_available: Optional[float] = None,
                 meta: Optional[dict] = None) -> Optional[Signal]:
    """Assemble a fully-formed Signal with sizing + 1:2 TP, or None if unsizable."""
    tp = take_profit_1to2(entry, stop, direction)
    units, notional, risk_amount = position_size(
        equity, entry, stop, risk_pct, capital_available
    )
    if units <= 0:
        return None
    rr = abs(tp - entry) / abs(entry - stop) if entry != stop else 0.0
    return Signal(
        strategy=strategy, symbol=symbol, direction=direction,
        entry=round(entry, 8), stop=round(stop, 8), take_profit=round(tp, 8),
        size=round(units, 8), notional=round(notional, 4),
        risk_amount=round(risk_amount, 4), rr=round(rr, 2),
        confidence=round(confidence, 1), reason=reason, meta=meta or {},
    )


# ---------------------------------------------------------------------------
# Strategy base
# ---------------------------------------------------------------------------
class Strategy:
    name = "base"
    # minimum bars required before the strategy is allowed to fire
    min_bars = 210

    def evaluate(self, df: pd.DataFrame, symbol: str, equity: float,
                 capital_available: Optional[float] = None,
                 risk_pct: float = 0.01) -> Optional[Signal]:
        raise NotImplementedError

    def _ready(self, df: pd.DataFrame) -> bool:
        return df is not None and len(df) >= self.min_bars


# ---------------------------------------------------------------------------
# Strategy 1 — Trend Confluence (Trend Following)
# ---------------------------------------------------------------------------
class TrendConfluence(Strategy):
    """
    Macro trend by the 200 EMA, pullback entries on the 50 EMA + RSI extreme,
    confirmed by OBV accumulation/distribution.

      LONG : close > EMA200 (uptrend) AND price has pulled back to/under EMA50
             AND RSI <= rsi_os  AND OBV rising (OBV > its own EMA)  -> buy the dip
      SHORT: close < EMA200 (downtrend) AND price has popped to/over EMA50
             AND RSI >= rsi_ob AND OBV falling                      -> sell the rip

    Stop: recent swing low/high, floored to be at least atr_floor * ATR away
          (so a noisy single-bar wick doesn't give us a stop that's too tight).
    """
    name = "trend_confluence"
    min_bars = 210

    def __init__(self, ema_macro=200, ema_pull=50, rsi_len=14,
                 rsi_os=30.0, rsi_ob=70.0, obv_ema=20,
                 swing_lookback=10, atr_len=14, atr_floor=1.5):
        self.ema_macro, self.ema_pull = ema_macro, ema_pull
        self.rsi_len, self.rsi_os, self.rsi_ob = rsi_len, rsi_os, rsi_ob
        self.obv_ema, self.swing_lookback = obv_ema, swing_lookback
        self.atr_len, self.atr_floor = atr_len, atr_floor

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None

        e_macro = ema(df["close"], self.ema_macro)
        e_pull = ema(df["close"], self.ema_pull)
        r = rsi(df["close"], self.rsi_len)
        ob = obv(df)
        ob_sig = ema(ob, self.obv_ema)
        a = atr(df, self.atr_len)

        close = float(df["close"].iloc[-1])
        last_atr = float(a.iloc[-1])
        ema_macro_v = float(e_macro.iloc[-1])
        ema_pull_v = float(e_pull.iloc[-1])
        rsi_v = float(r.iloc[-1])
        obv_rising = float(ob.iloc[-1]) > float(ob_sig.iloc[-1])

        # ---- LONG: uptrend pullback ----
        uptrend = close > ema_macro_v
        pulled_back = close <= ema_pull_v * 1.01  # at or just under the 50 EMA
        if uptrend and pulled_back and rsi_v <= self.rsi_os and obv_rising:
            swing = recent_swing_low(df, self.swing_lookback)
            stop = min(swing, close - self.atr_floor * last_atr)
            conf = 60 + min(30, (self.rsi_os - rsi_v))  # deeper oversold => more conviction
            return build_signal(
                self.name, symbol, "long", close, stop, equity, conf,
                f"Uptrend (>{self.ema_macro}EMA), pullback to {self.ema_pull}EMA, "
                f"RSI {rsi_v:.0f}<= {self.rsi_os:.0f}, OBV rising",
                risk_pct, capital_available,
                meta={"ema200": ema_macro_v, "ema50": ema_pull_v, "rsi": rsi_v,
                      "atr": last_atr, "swing_low": swing},
            )

        # ---- SHORT: downtrend rally ----
        downtrend = close < ema_macro_v
        popped = close >= ema_pull_v * 0.99
        if downtrend and popped and rsi_v >= self.rsi_ob and not obv_rising:
            swing = recent_swing_high(df, self.swing_lookback)
            stop = max(swing, close + self.atr_floor * last_atr)
            conf = 60 + min(30, (rsi_v - self.rsi_ob))
            return build_signal(
                self.name, symbol, "short", close, stop, equity, conf,
                f"Downtrend (<{self.ema_macro}EMA), rally to {self.ema_pull}EMA, "
                f"RSI {rsi_v:.0f}>= {self.rsi_ob:.0f}, OBV falling",
                risk_pct, capital_available,
                meta={"ema200": ema_macro_v, "ema50": ema_pull_v, "rsi": rsi_v,
                      "atr": last_atr, "swing_high": swing},
            )

        return None


# ---------------------------------------------------------------------------
# Strategy 2 — Volatility Squeeze (Breakout)
# ---------------------------------------------------------------------------
class VolatilitySqueeze(Strategy):
    """
    Bollinger Bands contracting INSIDE Keltner Channels marks a low-volatility
    coil. We arm on the squeeze and fire on the RELEASE bar, in the direction of
    the break, only if volume confirms expansion.

      squeeze_on : BB upper < KC upper AND BB lower > KC lower
      trigger    : squeeze was on within the last `arm_window` bars, is now off,
                   price closes beyond the BB mid in the break direction,
                   and volume > vol_spike * average volume.

    Stop: ATR-based (volatility just expanded, so we give it ATR room), not a
          swing — the breakout bar's swing is usually too tight.
    """
    name = "volatility_squeeze"
    min_bars = 60

    def __init__(self, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5,
                 vol_len=20, vol_spike=1.5, arm_window=6,
                 atr_len=14, atr_mult=2.0):
        self.bb_len, self.bb_mult = bb_len, bb_mult
        self.kc_len, self.kc_mult = kc_len, kc_mult
        self.vol_len, self.vol_spike = vol_len, vol_spike
        self.arm_window = arm_window
        self.atr_len, self.atr_mult = atr_len, atr_mult

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None

        bb_l, bb_m, bb_u = bollinger(df["close"], self.bb_len, self.bb_mult)
        kc_l, kc_m, kc_u = keltner(df, self.kc_len, self.kc_mult)
        a = atr(df, self.atr_len)
        vol_avg = sma(df["volume"], self.vol_len)

        squeeze_on = (bb_u < kc_u) & (bb_l > kc_l)

        # must currently be OFF but have been ON recently (a fresh release)
        if bool(squeeze_on.iloc[-1]):
            return None
        recently_armed = bool(squeeze_on.iloc[-(self.arm_window + 1):-1].any())
        if not recently_armed:
            return None

        close = float(df["close"].iloc[-1])
        mid = float(bb_m.iloc[-1])
        last_atr = float(a.iloc[-1])
        vol = float(df["volume"].iloc[-1])
        vol_ok = vol > self.vol_spike * float(vol_avg.iloc[-1])
        if not vol_ok:
            return None

        spike_ratio = vol / max(float(vol_avg.iloc[-1]), 1e-9)
        conf = 60 + min(30, (spike_ratio - self.vol_spike) * 20)

        if close > mid:   # bullish break
            stop = close - self.atr_mult * last_atr
            return build_signal(
                self.name, symbol, "long", close, stop, equity, conf,
                f"Squeeze release UP, vol {spike_ratio:.1f}x avg",
                risk_pct, capital_available,
                meta={"bb_mid": mid, "atr": last_atr, "vol_ratio": spike_ratio},
            )
        elif close < mid:  # bearish break
            stop = close + self.atr_mult * last_atr
            return build_signal(
                self.name, symbol, "short", close, stop, equity, conf,
                f"Squeeze release DOWN, vol {spike_ratio:.1f}x avg",
                risk_pct, capital_available,
                meta={"bb_mid": mid, "atr": last_atr, "vol_ratio": spike_ratio},
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 3 — Mean Reversion (Ranging)
# ---------------------------------------------------------------------------
class MeanReversion(Strategy):
    """
    Z-score of price deviation from rolling VWAP. Fade extremes:

      LONG  when z <= -z_entry  (price stretched far BELOW VWAP -> buy the dip)
      SHORT when z >= +z_entry  (price stretched far ABOVE VWAP -> sell the rip)

    NOTE: a mean-reversion trade's *natural* target is VWAP itself, which is
    usually well inside a 1:2 multiple. We still honor the strict 1:2 TP you
    asked for, but `meta["vwap_target"]` carries the mean so the Committee can
    choose to take profit earlier if it prefers. This strategy should be heavily
    down-weighted in a strong trend (z-score extremes persist in trends).
    """
    name = "mean_reversion"
    min_bars = 40

    def __init__(self, vwap_len=20, z_len=20, z_entry=2.0,
                 atr_len=14, atr_mult=1.5):
        self.vwap_len, self.z_len, self.z_entry = vwap_len, z_len, z_entry
        self.atr_len, self.atr_mult = atr_len, atr_mult

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None

        vwap = rolling_vwap(df, self.vwap_len)
        spread = df["close"] - vwap
        z = (spread - spread.rolling(self.z_len).mean()) / \
            spread.rolling(self.z_len).std(ddof=0)
        a = atr(df, self.atr_len)

        close = float(df["close"].iloc[-1])
        z_v = float(z.iloc[-1])
        vwap_v = float(vwap.iloc[-1])
        last_atr = float(a.iloc[-1])
        if np.isnan(z_v):
            return None

        conf = 60 + min(30, (abs(z_v) - self.z_entry) * 15)

        if z_v <= -self.z_entry:   # extreme dip -> long
            stop = close - self.atr_mult * last_atr
            return build_signal(
                self.name, symbol, "long", close, stop, equity, conf,
                f"Price {abs(z_v):.1f}sigma below VWAP -> mean-revert long",
                risk_pct, capital_available,
                meta={"z": z_v, "vwap": vwap_v, "vwap_target": vwap_v, "atr": last_atr},
            )
        elif z_v >= self.z_entry:  # over-extension -> short
            stop = close + self.atr_mult * last_atr
            return build_signal(
                self.name, symbol, "short", close, stop, equity, conf,
                f"Price {z_v:.1f}sigma above VWAP -> mean-revert short",
                risk_pct, capital_available,
                meta={"z": z_v, "vwap": vwap_v, "vwap_target": vwap_v, "atr": last_atr},
            )
        return None


# Registry the Committee will iterate over.
BATCH_1 = [TrendConfluence(), VolatilitySqueeze(), MeanReversion()]


# ---------------------------------------------------------------------------
# Demo / self-test
# ---------------------------------------------------------------------------
def _fetch_ohlcv_ccxt(exchange_id="binanceus", symbol="BTC/USDT",
                      timeframe="1h", limit=300) -> pd.DataFrame:
    """Optional live data via ccxt. Not used by the library itself."""
    import ccxt  # local import so the library has no hard ccxt dependency
    ex = getattr(ccxt, exchange_id)()
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")


def _synthetic_ohlcv(n=400, seed=7) -> pd.DataFrame:
    """Deterministic fake OHLCV with a trend + a squeeze + a spike, for testing."""
    rng = np.random.default_rng(seed)
    price = 100.0
    rows = []
    for i in range(n):
        drift = 0.06 if i < 250 else -0.04        # uptrend then downtrend
        vol_scale = 0.15 if 120 < i < 170 else 0.6  # low-vol coil mid-run
        step = drift + rng.normal(0, vol_scale)
        o = price
        c = max(1.0, price + step)
        hi = max(o, c) + abs(rng.normal(0, 0.3))
        lo = min(o, c) - abs(rng.normal(0, 0.3))
        v = abs(rng.normal(1000, 200)) * (4 if i == 172 else 1)  # volume spike at release
        rows.append([o, hi, lo, c, v])
        price = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


if __name__ == "__main__":
    try:
        df = _fetch_ohlcv_ccxt()
        src = "binanceus BTC/USDT 1h"
    except Exception as e:
        df = _synthetic_ohlcv()
        src = f"synthetic (ccxt unavailable: {type(e).__name__})"

    equity = 53.40            # matches your live wallet
    capital = 24.11           # free USDT
    print(f"Data source: {src}  |  bars={len(df)}  |  equity=${equity}\n")

    for strat in BATCH_1:
        sig = strat.evaluate(df, "BTC/USDT", equity, capital_available=capital)
        if sig:
            print(f"[FIRED] {sig.strategy:18} {sig.direction.upper():5} "
                  f"entry={sig.entry:.2f} stop={sig.stop:.2f} tp={sig.take_profit:.2f} "
                  f"R:R={sig.rr} size={sig.size:.4f} risk=${sig.risk_amount} "
                  f"conf={sig.confidence}")
            print(f"          {sig.reason}")
        else:
            print(f"[ flat ] {strat.name:18} no qualifying setup on last bar")
