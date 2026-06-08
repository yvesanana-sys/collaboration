"""
strategy_library_batch2.py  —  NovaTrade Strategy Library (Batch 2)
===================================================================

Strategies 4-7. Imports the shared framework from strategy_library so the
Signal contract, indicators, 1%-risk sizer and strict 1:2 TP stay single-source.
Still signal-only: nothing places an order.

  4. SmartMoneyConcepts   — Fair Value Gaps + liquidity sweeps (institutional)
  5. MacroDivergence      — price/oscillator divergence (momentum reversal)
  6. VolumeProfilePOC     — bounce off the highest-volume node (S/R)
  7. StatisticalPairTrading — cointegration spread, market-neutral (2-leg)

Strategy 7 has a DIFFERENT signature: evaluate_pair(df_a, df_b, ...) -> list[Signal]
because a pair trade is two linked legs, not one.
"""

from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd

from strategy_library import (
    Signal, Strategy, build_signal,
    ema, rsi, atr, sma, recent_swing_low, recent_swing_high,
)


# ---------------------------------------------------------------------------
# Extra primitives used only by Batch 2
# ---------------------------------------------------------------------------
def macd(close: pd.Series, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def find_pivots(series: pd.Series, k: int = 5, kind: str = "low") -> list[int]:
    """
    Indices of confirmed pivots. A pivot low at j is the strict min of the
    window [j-k, j+k]; pivot high is the max. Needs k bars on each side, so the
    most recent confirmable pivot is at most index len-1-k.
    """
    vals = series.values
    n = len(vals)
    out = []
    for j in range(k, n - k):
        window = vals[j - k:j + k + 1]
        if kind == "low" and vals[j] == window.min() and (window == vals[j]).sum() == 1:
            out.append(j)
        elif kind == "high" and vals[j] == window.max() and (window == vals[j]).sum() == 1:
            out.append(j)
    return out


# ---------------------------------------------------------------------------
# Strategy 4 — Smart Money Concepts / Order Blocks
# ---------------------------------------------------------------------------
class SmartMoneyConcepts(Strategy):
    """
    Enter where institutions hunt liquidity then reverse.

      Liquidity sweep (the trigger):
        bullish — the last bar's LOW pokes below a recent swing low (grabs
                  resting sell-stops) but CLOSES back above it (reclaim).
        bearish — last bar's HIGH pokes above a recent swing high but CLOSES
                  back below it.
      Fair Value Gap (the confirmation, boosts confidence):
        bullish FVG — an unfilled 3-bar imbalance where low[i] > high[i-2].
        bearish FVG — high[i] < low[i-2].

    Stop is SMC-native: just beyond the swept wick (where the liquidity grab
    failed), floored by ATR so it isn't absurdly tight.
    """
    name = "smc_order_blocks"
    min_bars = 60

    def __init__(self, swing_lookback=10, fvg_lookback=20,
                 atr_len=14, atr_floor=1.0):
        self.swing_lookback = swing_lookback
        self.fvg_lookback = fvg_lookback
        self.atr_len, self.atr_floor = atr_len, atr_floor

    def _has_bullish_fvg(self, df) -> bool:
        h, l = df["high"].values, df["low"].values
        for i in range(len(df) - self.fvg_lookback, len(df)):
            if i - 2 < 0:
                continue
            if l[i] > h[i - 2]:        # gap-up imbalance
                return True
        return False

    def _has_bearish_fvg(self, df) -> bool:
        h, l = df["high"].values, df["low"].values
        for i in range(len(df) - self.fvg_lookback, len(df)):
            if i - 2 < 0:
                continue
            if h[i] < l[i - 2]:        # gap-down imbalance
                return True
        return False

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        a = atr(df, self.atr_len)
        last_atr = float(a.iloc[-1])
        last = df.iloc[-1]
        swing_lo = recent_swing_low(df, self.swing_lookback)
        swing_hi = recent_swing_high(df, self.swing_lookback)
        close = float(last["close"])

        # ---- bullish sweep + reclaim ----
        swept_low = float(last["low"]) < swing_lo and close > swing_lo
        if swept_low:
            fvg = self._has_bullish_fvg(df)
            stop = min(float(last["low"]), close - self.atr_floor * last_atr)
            conf = 65 + (15 if fvg else 0)
            return build_signal(
                self.name, symbol, "long", close, stop, equity, conf,
                f"Bullish liquidity sweep + reclaim of swing low "
                f"{swing_lo:.4f}{' + FVG' if fvg else ''}",
                risk_pct, capital_available,
                meta={"swing_low": swing_lo, "bullish_fvg": fvg, "atr": last_atr},
            )

        # ---- bearish sweep + rejection ----
        swept_high = float(last["high"]) > swing_hi and close < swing_hi
        if swept_high:
            fvg = self._has_bearish_fvg(df)
            stop = max(float(last["high"]), close + self.atr_floor * last_atr)
            conf = 65 + (15 if fvg else 0)
            return build_signal(
                self.name, symbol, "short", close, stop, equity, conf,
                f"Bearish liquidity sweep + rejection of swing high "
                f"{swing_hi:.4f}{' + FVG' if fvg else ''}",
                risk_pct, capital_available,
                meta={"swing_high": swing_hi, "bearish_fvg": fvg, "atr": last_atr},
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 5 — Macro Oscillator Divergence
# ---------------------------------------------------------------------------
class MacroDivergence(Strategy):
    """
    Price/oscillator divergence on the LAST two pivots. Run this on a higher
    timeframe df (4H / 1D) for macro reversals.

      Bullish: price prints a LOWER low, MACD (and/or RSI) prints a HIGHER low.
      Bearish: price prints a HIGHER high, MACD (and/or RSI) prints a LOWER high.

    The second pivot must be recent (within `fresh` bars of the last close) so
    we're acting on a forming reversal, not stale history. Stop sits beyond the
    most recent pivot extreme.
    """
    name = "macro_divergence"
    min_bars = 90

    def __init__(self, rsi_len=14, pivot_k=5, fresh=8,
                 atr_len=14, atr_mult=1.0):
        self.rsi_len, self.pivot_k, self.fresh = rsi_len, pivot_k, fresh
        self.atr_len, self.atr_mult = atr_len, atr_mult

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        m_line, _, _ = macd(df["close"])
        r = rsi(df["close"], self.rsi_len)
        a = atr(df, self.atr_len)
        last_atr = float(a.iloc[-1])
        close = float(df["close"].iloc[-1])
        n = len(df)

        # ---- bullish divergence on pivot lows ----
        lows = find_pivots(df["low"], self.pivot_k, "low")
        if len(lows) >= 2:
            p1, p2 = lows[-2], lows[-1]
            recent = (n - 1 - p2) <= self.fresh
            price_ll = df["low"].iloc[p2] < df["low"].iloc[p1]
            osc_hl = (m_line.iloc[p2] > m_line.iloc[p1]) or (r.iloc[p2] > r.iloc[p1])
            both = (m_line.iloc[p2] > m_line.iloc[p1]) and (r.iloc[p2] > r.iloc[p1])
            if recent and price_ll and osc_hl:
                pivot_low = float(df["low"].iloc[p2])
                stop = pivot_low - self.atr_mult * last_atr
                conf = 65 + (15 if both else 0)
                return build_signal(
                    self.name, symbol, "long", close, stop, equity, conf,
                    "Bullish divergence: price lower-low, MACD/RSI higher-low",
                    risk_pct, capital_available,
                    meta={"pivot_low": pivot_low, "macd": float(m_line.iloc[p2]),
                          "rsi": float(r.iloc[p2]), "both_confirm": both},
                )

        # ---- bearish divergence on pivot highs ----
        highs = find_pivots(df["high"], self.pivot_k, "high")
        if len(highs) >= 2:
            p1, p2 = highs[-2], highs[-1]
            recent = (n - 1 - p2) <= self.fresh
            price_hh = df["high"].iloc[p2] > df["high"].iloc[p1]
            osc_lh = (m_line.iloc[p2] < m_line.iloc[p1]) or (r.iloc[p2] < r.iloc[p1])
            both = (m_line.iloc[p2] < m_line.iloc[p1]) and (r.iloc[p2] < r.iloc[p1])
            if recent and price_hh and osc_lh:
                pivot_high = float(df["high"].iloc[p2])
                stop = pivot_high + self.atr_mult * last_atr
                conf = 65 + (15 if both else 0)
                return build_signal(
                    self.name, symbol, "short", close, stop, equity, conf,
                    "Bearish divergence: price higher-high, MACD/RSI lower-high",
                    risk_pct, capital_available,
                    meta={"pivot_high": pivot_high, "macd": float(m_line.iloc[p2]),
                          "rsi": float(r.iloc[p2]), "both_confirm": both},
                )
        return None


# ---------------------------------------------------------------------------
# Strategy 6 — Volume Profile Point of Control
# ---------------------------------------------------------------------------
class VolumeProfilePOC(Strategy):
    """
    Build a volume-by-price profile over a lookback window; the POC is the
    price level that traded the most volume — a magnet / strong S/R.

      LONG : price is sitting at/just above the POC and the current bar is
             bullish (reclaiming the node) -> buy the bounce. Natural target is
             the next high-volume node above (carried in meta); TP still 1:2.
      SHORT: price at/just below the POC, current bar bearish -> rejection short.

    Stop is just beyond the POC (node fails) with an ATR buffer.
    """
    name = "volume_profile_poc"
    min_bars = 60

    def __init__(self, lookback=120, bins=24, tol_atr=0.6,
                 atr_len=14, atr_mult=1.5):
        self.lookback, self.bins, self.tol_atr = lookback, bins, tol_atr
        self.atr_len, self.atr_mult = atr_len, atr_mult

    def _profile(self, df):
        w = df.iloc[-self.lookback:]
        typical = ((w["high"] + w["low"] + w["close"]) / 3.0).values
        vol = w["volume"].values
        hist, edges = np.histogram(typical, bins=self.bins, weights=vol)
        centers = (edges[:-1] + edges[1:]) / 2.0
        poc = float(centers[int(np.argmax(hist))])
        return poc, centers, hist

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        a = atr(df, self.atr_len)
        last_atr = float(a.iloc[-1])
        poc, centers, hist = self._profile(df)
        last = df.iloc[-1]
        close = float(last["close"])
        bullish_bar = close > float(last["open"])
        near_poc = abs(close - poc) <= self.tol_atr * last_atr

        if not near_poc:
            return None

        nodes_above = centers[(centers > poc) & (hist > np.median(hist))]
        nodes_below = centers[(centers < poc) & (hist > np.median(hist))]

        if close >= poc and bullish_bar:                # bounce off POC support
            stop = poc - self.atr_mult * last_atr
            tgt = float(nodes_above.min()) if len(nodes_above) else None
            return build_signal(
                self.name, symbol, "long", close, stop, equity, 68,
                f"Bounce off POC {poc:.4f} (volume support)",
                risk_pct, capital_available,
                meta={"poc": poc, "next_node_up": tgt, "atr": last_atr},
            )
        if close <= poc and not bullish_bar:            # rejection at POC resistance
            stop = poc + self.atr_mult * last_atr
            tgt = float(nodes_below.max()) if len(nodes_below) else None
            return build_signal(
                self.name, symbol, "short", close, stop, equity, 68,
                f"Rejection at POC {poc:.4f} (volume resistance)",
                risk_pct, capital_available,
                meta={"poc": poc, "next_node_down": tgt, "atr": last_atr},
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 7 — Statistical Pair Trading (market-neutral, 2-leg)
# ---------------------------------------------------------------------------
class StatisticalPairTrading:
    """
    Trade the SPREAD between two cointegrated assets (e.g. BTC vs ETH).

      hedge ratio beta = OLS slope of log(A) on log(B)
      spread = log(A) - beta*log(B)
      z = (spread - rolling_mean) / rolling_std

      z >= +z_entry  -> spread too WIDE -> SHORT A (rich), LONG B (cheap)
      z <= -z_entry  -> spread too TIGHT -> LONG A (cheap), SHORT B (rich)

    HONEST CAVEAT: a strict 1:2 price R:R doesn't map cleanly onto a hedged
    spread trade — the *real* exit is the spread reverting (z -> ~0) with a
    stop if it keeps diverging (|z| >= z_stop). We still emit per-leg ATR stops
    and 1:2 TPs to satisfy the framework, but the z_target / z_stop in meta are
    the levels the Committee should actually manage the trade on. Risk is split
    ~half per leg so the combined book still risks ~1% of equity.
    """
    name = "pair_trading"
    min_bars = 120

    def __init__(self, z_len=60, z_entry=2.0, z_stop=3.5, corr_min=0.7,
                 atr_len=14, atr_mult=2.0):
        self.z_len, self.z_entry, self.z_stop = z_len, z_entry, z_stop
        self.corr_min = corr_min
        self.atr_len, self.atr_mult = atr_len, atr_mult

    def _ready(self, df_a, df_b) -> bool:
        return (df_a is not None and df_b is not None
                and len(df_a) >= self.min_bars and len(df_b) >= self.min_bars)

    def evaluate_pair(self, df_a, df_b, sym_a, sym_b, equity,
                      capital_available=None, risk_pct=0.01) -> list[Signal]:
        if not self._ready(df_a, df_b):
            return []

        n = min(len(df_a), len(df_b))
        a = df_a.iloc[-n:].reset_index(drop=True)
        b = df_b.iloc[-n:].reset_index(drop=True)

        # correlation gate (proxy for "highly correlated / cointegrated")
        ra, rb = a["close"].pct_change(), b["close"].pct_change()
        corr = ra.corr(rb)
        if pd.isna(corr) or corr < self.corr_min:
            return []

        la, lb = np.log(a["close"]), np.log(b["close"])
        beta = float(np.polyfit(lb, la, 1)[0])
        spread = la - beta * lb
        mean = spread.rolling(self.z_len).mean()
        std = spread.rolling(self.z_len).std(ddof=0)
        z = (spread - mean) / std
        z_now = float(z.iloc[-1])
        if np.isnan(z_now) or abs(z_now) < self.z_entry:
            return []

        # optional proper cointegration test if statsmodels is present
        coint_p = None
        try:
            from statsmodels.tsa.stattools import adfuller
            coint_p = float(adfuller(spread.dropna())[1])
        except Exception:
            pass

        atr_a = float(atr(a, self.atr_len).iloc[-1])
        atr_b = float(atr(b, self.atr_len).iloc[-1])
        ca = float(a["close"].iloc[-1])
        cb = float(b["close"].iloc[-1])
        leg_risk = risk_pct / 2.0
        cap_leg = (capital_available / 2.0) if capital_available else None
        meta = {"z": z_now, "beta": beta, "corr": float(corr),
                "z_entry": self.z_entry, "z_stop": self.z_stop,
                "z_target": 0.0, "coint_adf_p": coint_p, "pair": f"{sym_a}/{sym_b}"}

        if z_now >= self.z_entry:        # short A, long B
            dir_a, dir_b = "short", "long"
        else:                            # long A, short B
            dir_a, dir_b = "long", "short"

        stop_a = ca + self.atr_mult * atr_a if dir_a == "short" else ca - self.atr_mult * atr_a
        stop_b = cb + self.atr_mult * atr_b if dir_b == "short" else cb - self.atr_mult * atr_b
        conf = 60 + min(30, (abs(z_now) - self.z_entry) * 15)

        sig_a = build_signal(self.name, sym_a, dir_a, ca, stop_a, equity, conf,
                             f"Pair leg vs {sym_b}: z={z_now:.2f} (revert to 0)",
                             leg_risk, cap_leg, meta={**meta, "leg": "A"})
        sig_b = build_signal(self.name, sym_b, dir_b, cb, stop_b, equity, conf,
                             f"Pair leg vs {sym_a}: z={z_now:.2f} (revert to 0)",
                             leg_risk, cap_leg, meta={**meta, "leg": "B"})
        return [s for s in (sig_a, sig_b) if s is not None]


# single-asset strategies (the Committee iterates these like Batch 1)
BATCH_2 = [SmartMoneyConcepts(), MacroDivergence(), VolumeProfilePOC()]
# pair strategy is invoked separately because it needs two DataFrames
PAIR_STRATEGY = StatisticalPairTrading()


if __name__ == "__main__":
    from strategy_library import _synthetic_ohlcv
    eq, cap = 53.40, 24.11
    df = _synthetic_ohlcv(n=400)
    print(f"Single-asset strategies on synthetic data (bars={len(df)}):")
    for s in BATCH_2:
        sig = s.evaluate(df, "BTC/USDT", eq, capital_available=cap)
        print(f"  {s.name:20} -> {'FIRED ' + sig.direction.upper() if sig else 'flat'}")

    # build a correlated second asset for the pair demo
    df2 = df.copy()
    df2[["open", "high", "low", "close"]] *= 0.6  # ETH-ish scaled, correlated
    legs = PAIR_STRATEGY.evaluate_pair(df, df2, "BTC/USDT", "ETH/USDT", eq, cap)
    print(f"  pair_trading        -> {len(legs)} legs",
          [f'{s.symbol}:{s.direction}' for s in legs])
