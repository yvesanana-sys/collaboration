"""
strategy_library_batch3.py  —  NovaTrade Strategy Library (Batch 3)
===================================================================

Strategies 8-10, plus they complete the single-asset roster. Same framework,
same contract, still signal-only.

  8.  GMMA              — Guppy ribbon alignment/expansion (trend strength)
  9.  OpeningRangeBreakout — first-of-session range break (intraday momentum)
  10. DynamicDCA        — std-band accumulation sleeve (bear-market building)

CAVEAT on #10: DCA is an accumulation sleeve, not a stop-managed trade. It is
BUY-ONLY and stop-less by design — you hold through drawdown and average down.
To fit the framework's sizer it carries a nominal structural stop (the next
deeper std band) and a tier-scaled size, but the REAL management is in
meta["tier"] / meta["accumulate_fraction"]. The Committee should treat it as a
separate horizon bucket, not vote it against the tactical strategies.
"""

from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd

from strategy_library import (
    Signal, Strategy, build_signal, ema, sma, atr,
    recent_swing_low, recent_swing_high,
)


# ---------------------------------------------------------------------------
# Strategy 8 — GMMA (Guppy Multiple Moving Average)
# ---------------------------------------------------------------------------
class GMMA(Strategy):
    """
    Two EMA ribbons:
      short (traders):     3, 5, 8, 10, 12, 15
      long  (investors):  30,35,40,45,50,60

    Bullish alignment = the WHOLE short ribbon sits above the WHOLE long ribbon
    (min(short) > max(long)). The high-conviction entry is a *fresh* alignment:
    the short ribbon compressed toward / into the long ribbon recently (a retail
    pullback) and has just re-expanded above it (institutions held) — trend
    continuation, not a fresh trend.

    Stop: below the long ribbon (max of long EMAs) for longs, ATR-floored.
    """
    name = "gmma_ribbon"
    min_bars = 80

    SHORT = (3, 5, 8, 10, 12, 15)
    LONG = (30, 35, 40, 45, 50, 60)

    def __init__(self, compress_window=6, atr_len=14, atr_floor=1.0):
        self.compress_window = compress_window
        self.atr_len, self.atr_floor = atr_len, atr_floor

    def _ribbons(self, close):
        s = pd.DataFrame({p: ema(close, p) for p in self.SHORT})
        l = pd.DataFrame({p: ema(close, p) for p in self.LONG})
        return s, l

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        close = df["close"]
        s, l = self._ribbons(close)
        a = atr(df, self.atr_len)
        last_atr = float(a.iloc[-1])
        c = float(close.iloc[-1])

        smin = s.min(axis=1)   # bottom of short ribbon
        smax = s.max(axis=1)   # top of short ribbon
        lmin = l.min(axis=1)
        lmax = l.max(axis=1)
        long_rising = float(lmin.iloc[-1]) > float(lmin.iloc[-5])

        bull_now = float(smin.iloc[-1]) > float(lmax.iloc[-1])
        bear_now = float(smax.iloc[-1]) < float(lmin.iloc[-1])
        # "fresh" = not aligned somewhere in the recent compression window
        win = slice(-(self.compress_window + 1), -1)
        bull_fresh = bull_now and (smin.iloc[win] <= lmax.iloc[win]).any()
        bear_fresh = bear_now and (smax.iloc[win] >= lmin.iloc[win]).any()

        if bull_fresh and long_rising:
            stop = min(float(lmax.iloc[-1]), c - self.atr_floor * last_atr)
            sep = (float(smin.iloc[-1]) - float(lmax.iloc[-1])) / c
            conf = 65 + min(20, sep * 4000)
            return build_signal(
                self.name, symbol, "long", c, stop, equity, conf,
                "GMMA bullish: short ribbon re-expanded above rising long ribbon",
                risk_pct, capital_available,
                meta={"sep_pct": round(sep * 100, 3), "long_top": float(lmax.iloc[-1]),
                      "atr": last_atr},
            )
        if bear_fresh and not long_rising:
            stop = max(float(lmin.iloc[-1]), c + self.atr_floor * last_atr)
            sep = (float(lmin.iloc[-1]) - float(smax.iloc[-1])) / c
            conf = 65 + min(20, sep * 4000)
            return build_signal(
                self.name, symbol, "short", c, stop, equity, conf,
                "GMMA bearish: short ribbon re-expanded below falling long ribbon",
                risk_pct, capital_available,
                meta={"sep_pct": round(sep * 100, 3), "long_bot": float(lmin.iloc[-1]),
                      "atr": last_atr},
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 9 — Opening Range Breakout (intraday momentum)
# ---------------------------------------------------------------------------
class OpeningRangeBreakout(Strategy):
    """
    The first `or_bars` of a session set a high/low box. The first clean break
    of that box, in the break direction, is the entry.

    Session boundary:
      - if df has a DatetimeIndex -> group by calendar date, use the latest day.
      - else (plain index)        -> treat the last `session_len` bars as the
                                      current session (fallback for 24/7 feeds).

    Stop: opposite side of the opening range (box low for longs), ATR-floored.
    Only the FIRST breakout bar fires (we require the prior bar to be inside).
    """
    name = "opening_range_breakout"
    min_bars = 30

    def __init__(self, or_bars=6, session_len=24, atr_len=14, atr_floor=0.5):
        self.or_bars = or_bars
        self.session_len = session_len
        self.atr_len, self.atr_floor = atr_len, atr_floor

    def _session(self, df) -> pd.DataFrame:
        if isinstance(df.index, pd.DatetimeIndex):
            last_day = df.index[-1].date()
            day = df[df.index.date == last_day]
            return day if len(day) > self.or_bars else df.iloc[-self.session_len:]
        return df.iloc[-self.session_len:]

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        sess = self._session(df)
        if len(sess) <= self.or_bars + 1:
            return None

        opening = sess.iloc[:self.or_bars]
        or_high = float(opening["high"].max())
        or_low = float(opening["low"].min())
        a = atr(df, self.atr_len)
        last_atr = float(a.iloc[-1])

        last = sess.iloc[-1]
        prev = sess.iloc[-2]
        c = float(last["close"])
        prev_inside = or_low <= float(prev["close"]) <= or_high

        if not prev_inside:           # only the FIRST break fires
            return None

        if c > or_high:               # bullish break
            stop = min(or_low, c - self.atr_floor * last_atr)
            return build_signal(
                self.name, symbol, "long", c, stop, equity, 66,
                f"Break above opening range high {or_high:.4f}",
                risk_pct, capital_available,
                meta={"or_high": or_high, "or_low": or_low, "atr": last_atr},
            )
        if c < or_low:                # bearish break
            stop = max(or_high, c + self.atr_floor * last_atr)
            return build_signal(
                self.name, symbol, "short", c, stop, equity, 66,
                f"Break below opening range low {or_low:.4f}",
                risk_pct, capital_available,
                meta={"or_high": or_high, "or_low": or_low, "atr": last_atr},
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 10 — Dynamic DCA Accumulation (accumulation sleeve)
# ---------------------------------------------------------------------------
class DynamicDCA(Strategy):
    """
    Long-horizon SPOT accumulation. The further price falls below the 200-period
    MA (measured in standard deviations), the larger the buy. BUY-ONLY.

    tier = floor of std-distance below the MA (1, 2, 3, ...). accumulate_fraction
    scales with tier. There is NO tactical stop — you are building a position to
    hold. The "stop" emitted is a structural catastrophe level (next deeper band)
    only so the framework's sizer has a number; real management is tiered DCA.
    Keep this in its own capital bucket — do NOT let the Committee net it against
    short-side tactical signals.
    """
    name = "dynamic_dca"
    min_bars = 210

    def __init__(self, ma_len=200, max_tier=4, base_fraction=0.10):
        self.ma_len, self.max_tier, self.base_fraction = ma_len, max_tier, base_fraction

    def evaluate(self, df, symbol, equity, capital_available=None, risk_pct=0.01):
        if not self._ready(df):
            return None
        close = df["close"]
        ma = sma(close, self.ma_len)
        sd = close.rolling(self.ma_len).std(ddof=0)
        c = float(close.iloc[-1])
        ma_v = float(ma.iloc[-1])
        sd_v = float(sd.iloc[-1])
        if sd_v <= 0 or c >= ma_v:           # only accumulate below the mean
            return None

        std_below = (ma_v - c) / sd_v
        tier = int(min(self.max_tier, np.floor(std_below)))
        if tier < 1:
            return None

        frac = min(1.0, self.base_fraction * tier)         # deeper => bigger buy
        # nominal structural stop = next deeper band (sizer needs a distance)
        nominal_stop = ma_v - (tier + 2) * sd_v
        sig = build_signal(
            self.name, symbol, "long", c, nominal_stop, equity,
            55 + tier * 8,
            f"DCA tier {tier}: price {std_below:.1f}sigma below {self.ma_len}MA",
            risk_pct, capital_available,
            meta={"tier": tier, "std_below": round(std_below, 2),
                  "ma": ma_v, "accumulate_fraction": round(frac, 3),
                  "sleeve": "accumulation", "stopless": True},
        )
        # override the framework size with the tier-scaled accumulation size
        if sig and capital_available:
            spend = capital_available * frac
            sig.size = round(spend / c, 8)
            sig.notional = round(spend, 4)
        return sig


BATCH_3 = [GMMA(), OpeningRangeBreakout(), DynamicDCA()]


if __name__ == "__main__":
    from strategy_library import _synthetic_ohlcv
    df = _synthetic_ohlcv(n=400)
    print(f"Batch 3 on synthetic data (bars={len(df)}):")
    for s in BATCH_3:
        sig = s.evaluate(df, "BTC/USDT", 53.40, capital_available=24.11)
        print(f"  {s.name:22} -> {'FIRED ' + sig.direction.upper() if sig else 'flat'}")
