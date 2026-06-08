"""
committee.py  —  NovaTrade Regime Detector + Strategy Committee
===============================================================

This is the brain that sits above the Strategy Library. It does two jobs:

  1. RegimeDetector — classify the current tape (bull_trend / bear_trend /
     ranging / high_vol) from ADX, the 200-EMA slope, ATR percentile and
     Bollinger bandwidth percentile.

  2. Committee — collect every strategy's signal for a symbol, weight each by
     how much the current regime trusts that strategy (the weighting matrix),
     require CONFLUENCE (enough agreeing voices + a minimum aggregate score),
     net out conflicts, and emit ONE Decision per symbol — or stand aside.

Still no order placement. The Committee outputs a Decision; wiring a Decision
to a broker is a separate, human-gated step.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd

from strategy_library import BATCH_1, atr, ema, sma
from strategy_library_batch2 import BATCH_2
from strategy_library_batch3 import BATCH_3

# every single-asset strategy the Committee listens to
ALL_STRATEGIES = BATCH_1 + BATCH_2 + BATCH_3


# ---------------------------------------------------------------------------
# Weighting matrix:  regime -> strategy.name -> weight class
#   prio = 1.0   mid = 0.6   low = 0.3   veto = 0.0
# ---------------------------------------------------------------------------
WCLASS = {"prio": 1.0, "mid": 0.6, "low": 0.3, "veto": 0.0}

MATRIX = {
    # strategy name           bull_trend  bear_trend  ranging    high_vol
    "trend_confluence":       ("prio",    "prio",     "veto",    "mid"),
    "volatility_squeeze":     ("low",     "low",      "low",     "prio"),
    "mean_reversion":         ("veto",    "veto",     "prio",    "low"),
    "smc_order_blocks":       ("prio",    "prio",     "mid",     "mid"),
    "macro_divergence":       ("veto",    "veto",     "low",     "prio"),
    "volume_profile_poc":     ("low",     "low",      "prio",    "mid"),
    "gmma_ribbon":            ("prio",    "prio",     "veto",    "low"),
    "opening_range_breakout": ("mid",     "mid",      "low",     "prio"),
    # pair_trading is market-neutral; steady small weight everywhere
    "pair_trading":           ("mid",     "mid",      "mid",     "mid"),
    # dynamic_dca is a separate sleeve — excluded from the tactical vote
    "dynamic_dca":            ("veto",    "veto",     "veto",    "veto"),
}
REGIMES = ("bull_trend", "bear_trend", "ranging", "high_vol")


def weight_for(strategy_name: str, regime: str) -> float:
    row = MATRIX.get(strategy_name)
    if not row:
        return 0.0
    return WCLASS[row[REGIMES.index(regime)]]


# ---------------------------------------------------------------------------
# Regime detector
# ---------------------------------------------------------------------------
@dataclass
class Regime:
    label: str
    adx: float
    ema_slope: float
    atr_pct: float
    bb_bandwidth_pctile: float

    def display(self) -> str:
        return {"bull_trend": "Strong Trend", "bear_trend": "Strong Trend",
                "ranging": "Ranging", "high_vol": "High Volatility"}[self.label]


class RegimeDetector:
    def __init__(self, adx_len=14, ema_len=200, atr_len=14,
                 bb_len=20, hist=100, adx_trend=25.0, atr_hot=0.85, bb_squeeze=0.20):
        self.adx_len, self.ema_len, self.atr_len = adx_len, ema_len, atr_len
        self.bb_len, self.hist = bb_len, hist
        self.adx_trend, self.atr_hot, self.bb_squeeze = adx_trend, atr_hot, bb_squeeze

    def _adx(self, df):
        up = df["high"].diff()
        dn = -df["low"].diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
        tr = atr(df, self.adx_len).replace(0, np.nan)
        pdi = 100 * plus_dm.ewm(alpha=1/self.adx_len, adjust=False).mean() / tr
        mdi = 100 * minus_dm.ewm(alpha=1/self.adx_len, adjust=False).mean() / tr
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        return float(dx.ewm(alpha=1/self.adx_len, adjust=False).mean().iloc[-1])

    def detect(self, df: pd.DataFrame) -> Regime:
        if df is None or len(df) < max(self.ema_len, self.hist) // 2:
            return Regime("ranging", 0, 0, 0, 0.5)
        close = df["close"]
        a = atr(df, self.atr_len)
        atr_ratio = a / close
        atr_pct = float(atr_ratio.iloc[-1])
        atr_pctile = float(atr_ratio.rolling(self.hist).rank(pct=True).iloc[-1])

        e = ema(close, self.ema_len)
        ema_slope = float((e.iloc[-1] - e.iloc[-10]) / e.iloc[-10]) if len(e) > 10 else 0.0

        mid = sma(close, self.bb_len)
        sd = close.rolling(self.bb_len).std(ddof=0)
        bw = (2 * 2 * sd) / mid
        bb_pctile = float(bw.rolling(self.hist).rank(pct=True).iloc[-1])

        try:
            adx = self._adx(df)
        except Exception:
            adx = 0.0

        # classification — volatility first, then trend, else range
        if not np.isnan(atr_pctile) and atr_pctile > self.atr_hot:
            label = "high_vol"
        elif adx >= self.adx_trend:
            label = "bull_trend" if ema_slope >= 0 else "bear_trend"
        else:
            label = "ranging"
        return Regime(label, round(adx, 1), round(ema_slope, 5),
                      round(atr_pct, 5),
                      round(bb_pctile if not np.isnan(bb_pctile) else 0.5, 3))


# ---------------------------------------------------------------------------
# Committee
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    symbol: str
    action: str               # "long" | "short" | "stand_aside"
    score: float              # signed weighted conviction
    regime: str
    n_agree: int
    contributors: list = field(default_factory=list)   # [(strategy, dir, weighted_contrib)]
    entry: Optional[float] = None
    stop: Optional[float] = None
    take_profit: Optional[float] = None
    size: Optional[float] = None
    rationale: str = ""

    def as_dict(self):
        d = self.__dict__.copy()
        return d


class Committee:
    """
    Aggregate per-symbol signals -> one Decision.

      contribution(signal) = (confidence/100) * regime_weight(strategy)
      score = sum( +contribution for longs, -contribution for shorts )
      ACT only if:
        - at least `min_agree` strategies (nonzero weight) agree on the winning side
        - and |score| >= `min_score`
      otherwise stand aside.

    Execution levels come from the highest-weighted contributing signal on the
    winning side (its dynamic stop / 1:2 TP / size are already framework-correct).
    """
    def __init__(self, detector: Optional[RegimeDetector] = None,
                 min_agree=2, min_score=0.8, risk_pct=0.01):
        self.detector = detector or RegimeDetector()
        self.min_agree, self.min_score, self.risk_pct = min_agree, min_score, risk_pct

    def decide(self, df: pd.DataFrame, symbol: str, equity: float,
               capital_available: Optional[float] = None) -> Decision:
        regime = self.detector.detect(df)
        longs, shorts, contributors = [], [], []

        for strat in ALL_STRATEGIES:
            w = weight_for(strat.name, regime.label)
            if w <= 0:                      # vetoed/silenced this regime
                continue
            try:
                sig = strat.evaluate(df, symbol, equity, capital_available, self.risk_pct)
            except Exception:
                sig = None
            if not sig:
                continue
            contrib = (sig.confidence / 100.0) * w
            contributors.append((strat.name, sig.direction, round(contrib, 3)))
            (longs if sig.direction == "long" else shorts).append((contrib, w, sig))

        score = sum(c for c, _, _ in longs) - sum(c for c, _, _ in shorts)
        n_long, n_short = len(longs), len(shorts)

        # decide winning side by sign of score + confluence count on that side
        if score >= self.min_score and n_long >= self.min_agree:
            side, pool = "long", longs
        elif score <= -self.min_score and n_short >= self.min_agree:
            side, pool = "short", shorts
        else:
            return Decision(symbol, "stand_aside", round(score, 3), regime.label,
                            max(n_long, n_short), contributors,
                            rationale=f"No confluence (long={n_long}, short={n_short}, "
                                      f"score={score:.2f}, need >={self.min_agree} "
                                      f"& |score|>={self.min_score})")

        # levels from the highest-weighted signal on the winning side
        _, _, lead = max(pool, key=lambda t: t[1])
        return Decision(symbol, side, round(score, 3), regime.label, len(pool),
                        contributors, entry=lead.entry, stop=lead.stop,
                        take_profit=lead.take_profit, size=lead.size,
                        rationale=f"{regime.display()} regime; {len(pool)} strategies "
                                  f"agree {side}; lead={lead.strategy}")


if __name__ == "__main__":
    from strategy_library import _synthetic_ohlcv
    det = RegimeDetector()
    com = Committee()
    for seed in (7, 11, 21):
        df = _synthetic_ohlcv(n=400, seed=seed)
        r = det.detect(df)
        d = com.decide(df, "BTC/USDT", 53.40, capital_available=24.11)
        print(f"seed {seed}: regime={r.label:11} adx={r.adx:5} "
              f"-> {d.action:11} score={d.score:+.2f} agree={d.n_agree} "
              f"contributors={d.contributors}")
