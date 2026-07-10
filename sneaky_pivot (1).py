#!/usr/bin/env python3
"""
sneaky_pivot.py — the Sneaky Pivot strategy ENGINE (pure logic + backtest).

This is deliberately standalone and dependency-free (pure Python). It knows
nothing about brokers, AIs, or the bot. You feed it 15m OHLC candles for ONE
symbol and it tells you the current levels and any entry/stop/TP signal, applying
the mechanical rules from SNEAKY_PIVOT_SPEC.md.

Because it's isolated:
  - it's small and safe to upload / commit anywhere,
  - it can be unit-tested and backtested BEFORE touching the live bot,
  - it's identical no matter how the AI layer (Claude/Grok symbol selection +
    deconfliction) is wired on top — that layer just chooses which symbols to
    feed in here.

Run the built-in demo backtest:
    python sneaky_pivot.py --demo
Backtest a CSV (columns: timestamp,open,high,low,close[,volume]):
    python sneaky_pivot.py --csv path/to/candles.csv
"""
from __future__ import annotations
import argparse
import csv
import math
import random
from dataclasses import dataclass, field
from typing import Optional


# ── Data types ──────────────────────────────────────────────
@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def rng(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open


@dataclass
class Levels:
    sh: float   # swing high  = ORC high
    rh: float   # range high  = ORC body top
    rl: float   # range low   = ORC body bottom
    sl: float   # swing low   = ORC low

    @property
    def mid(self) -> float:
        return (self.sh + self.sl) / 2.0


@dataclass
class Config:
    orc_mult: float = 1.8
    orc_lookback: int = 20
    atr_period: int = 14
    touch_atr: float = 0.10
    stop_atr: float = 0.25
    entry_on_break: bool = False
    direction: str = "long_only"          # "long_only" | "long_short"
    tp_fractions: tuple = (0.4, 0.4, 0.2)
    range_max_bars: int = 32


@dataclass
class Signal:
    action: str                            # "BUY" | "SELL" | "NONE"
    price: float = 0.0
    stop: float = 0.0
    targets: tuple = ()
    level: str = ""                        # which level was hit (RL/SL/RH/SH)
    reason: str = ""


# ── Indicators ──────────────────────────────────────────────
def atr(candles: list[Candle], period: int) -> float:
    if len(candles) < 2:
        return candles[-1].rng if candles else 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def avg_range(candles: list[Candle], lookback: int) -> float:
    w = candles[-lookback:] if len(candles) >= lookback else candles
    return sum(c.rng for c in w) / len(w) if w else 0.0


# ── Core strategy pieces ────────────────────────────────────
def is_orc(candles: list[Candle], i: int, cfg: Config) -> bool:
    """Does candle[i] qualify as an Opening Range Candle? (expansion + dominance)"""
    if i < cfg.orc_lookback:
        return False
    prior = candles[i - cfg.orc_lookback:i]
    a = sum(c.rng for c in prior) / len(prior)
    if a <= 0:
        return False
    if candles[i].rng < cfg.orc_mult * a:
        return False
    # dominance: biggest range in the lookback window incl. self
    window = candles[i - cfg.orc_lookback + 1:i + 1]
    return candles[i].rng >= max(c.rng for c in window)


def levels_from_orc(orc: Candle) -> Levels:
    return Levels(
        sh=orc.high,
        rh=max(orc.open, orc.close),
        rl=min(orc.open, orc.close),
        sl=orc.low,
    )


def sneaky_at_level(c: Candle, lv: Levels, tol: float, cfg: Config):
    """
    Return ('long', level_name) / ('short', level_name) / (None, None).
    Long  = wick through RL/SL but close back above, bullish-ish.
    Short = wick through RH/SH but close back below, bearish-ish.
    Prefer the deeper level when both are touched.
    """
    upper_half_close = c.close >= (c.high + c.low) / 2
    lower_half_close = c.close <= (c.high + c.low) / 2

    # Long checks — deeper (SL) first
    for name, level in (("SL", lv.sl), ("RL", lv.rl)):
        if c.low <= level + tol and c.close > level and (c.bullish or upper_half_close):
            return "long", name
    # Short checks — deeper (SH) first
    for name, level in (("SH", lv.sh), ("RH", lv.rh)):
        if c.high >= level - tol and c.close < level and ((not c.bullish) or lower_half_close):
            return "short", name
    return None, None


def _entry_signal(direction: str, level_name: str, lv: Levels, c: Candle, cfg: Config) -> Signal:
    a = atr([c], cfg.atr_period)  # local fallback; caller passes better atr below
    buf = cfg.stop_atr * a
    if direction == "long":
        entry = c.close if not cfg.entry_on_break else c.high
        if level_name == "RL":
            stop = lv.sl - buf
        else:  # SL
            stop = lv.sl - buf
        targets = (lv.mid, lv.rh, lv.sh) if level_name in ("SL",) else (lv.mid, lv.rh, lv.sh)
        return Signal("BUY", entry, stop, targets, level_name, f"bullish sneaky at {level_name}")
    else:
        entry = c.close if not cfg.entry_on_break else c.low
        stop = lv.sh + buf
        targets = (lv.mid, lv.rl, lv.sl)
        return Signal("SELL", entry, stop, targets, level_name, f"bearish sneaky at {level_name}")


# ── Backtester ──────────────────────────────────────────────
@dataclass
class Trade:
    side: str
    entry: float
    stop: float
    targets: tuple
    level: str
    entry_i: int
    risk: float = 0.0                      # fixed at entry — never recompute from moved stop
    remaining: float = 1.0
    realized_r: float = 0.0
    be_moved: bool = False


def backtest(candles: list[Candle], cfg: Config) -> dict:
    """Bar-by-bar simulation. Conservative: if stop & TP both hit in a bar, stop first."""
    trades_closed = []
    orc_i: Optional[int] = None
    lv: Optional[Levels] = None
    pos: Optional[Trade] = None

    def close_trade(t: Trade):
        trades_closed.append(t)

    for i in range(len(candles)):
        c = candles[i]

        # ---- manage an open position on THIS bar ----
        if pos is not None:
            risk = pos.risk or 1e-9                       # FIXED at entry
            # stop first (conservative)
            hit_stop = (pos.side == "long" and c.low <= pos.stop) or \
                       (pos.side == "short" and c.high >= pos.stop)
            if hit_stop:
                r = (pos.stop - pos.entry) / risk if pos.side == "long" else (pos.entry - pos.stop) / risk
                pos.realized_r += r * pos.remaining
                pos.remaining = 0.0
                close_trade(pos); pos = None
            else:
                # take-profits in order
                for k, tgt in enumerate(pos.targets):
                    if pos.remaining <= 0:
                        break
                    frac = cfg.tp_fractions[k] if k < len(cfg.tp_fractions) else 0.0
                    frac = min(frac, pos.remaining)
                    hit = (pos.side == "long" and c.high >= tgt) or \
                          (pos.side == "short" and c.low <= tgt)
                    if hit and frac > 0:
                        r = (tgt - pos.entry) / risk if pos.side == "long" else (pos.entry - tgt) / risk
                        pos.realized_r += r * frac
                        pos.remaining -= frac
                        if not pos.be_moved:            # move stop to breakeven after first TP
                            pos.stop = pos.entry
                            pos.be_moved = True
                if pos and pos.remaining <= 1e-9:
                    close_trade(pos); pos = None

        # ---- range invalidation ----
        if lv is not None and orc_i is not None:
            a = atr(candles[:i + 1], cfg.atr_period)
            buf = cfg.stop_atr * a
            broke = c.close > lv.sh + buf or c.close < lv.sl - buf
            expired = (i - orc_i) > cfg.range_max_bars
            if (broke or expired) and pos is None:
                lv = None; orc_i = None

        # ---- detect / refresh ORC (only when flat) ----
        if pos is None and is_orc(candles, i, cfg):
            orc_i = i
            lv = levels_from_orc(candles[i])
            continue  # sneaky comes on a LATER candle, never the ORC itself

        # ---- look for a sneaky entry ----
        if pos is None and lv is not None and i > (orc_i or 0):
            a = atr(candles[:i + 1], cfg.atr_period)
            tol = cfg.touch_atr * a
            side, name = sneaky_at_level(c, lv, tol, cfg)
            if side == "short" and cfg.direction == "long_only":
                side = None  # long-only: bearish sneaky is an exit signal, no new short
            if side:
                buf = cfg.stop_atr * a
                if side == "long":
                    entry = c.close if not cfg.entry_on_break else c.high
                    stop = lv.sl - buf
                    targets = (lv.mid, lv.rh, lv.sh)
                else:
                    entry = c.close if not cfg.entry_on_break else c.low
                    stop = lv.sh + buf
                    targets = (lv.mid, lv.rl, lv.sl)
                pos = Trade(side, entry, stop, targets, name, i, risk=abs(entry - stop))

    # ---- stats ----
    n = len(trades_closed)
    rs = [t.realized_r for t in trades_closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total_r = sum(rs)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # max drawdown in R
    peak = 0.0; cum = 0.0; mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "total_R": total_r,
        "avg_R": (total_r / n) if n else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_drawdown_R": mdd,
    }


# ── Demo data + CLI ─────────────────────────────────────────
def synth_candles(n=1500, seed=7) -> list[Candle]:
    """Random-walk with occasional expansion candles (ORCs) and mean-reverting ranges."""
    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        # occasionally inject a big expansion candle
        big = rng.random() < 0.02
        drift = rng.uniform(-0.15, 0.15)
        body = rng.uniform(0.05, 0.35) * (6.0 if big else 1.0)
        o = price
        direction = 1 if rng.random() < 0.5 else -1
        c = o + direction * body + drift
        hi = max(o, c) + rng.uniform(0.02, 0.25) * (3.0 if big else 1.0)
        lo = min(o, c) - rng.uniform(0.02, 0.25) * (3.0 if big else 1.0)
        out.append(Candle(i, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), rng.uniform(100, 1000)))
        price = c
    return out


def load_csv(path: str) -> list[Candle]:
    out = []
    with open(path) as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r):
            def g(*keys):
                for k in keys:
                    if k in row and row[k] not in ("", None):
                        return float(row[k])
                raise KeyError(keys)
            out.append(Candle(
                ts=int(float(row.get("timestamp", i))),
                open=g("open", "Open", "o"), high=g("high", "High", "h"),
                low=g("low", "Low", "l"), close=g("close", "Close", "c"),
                volume=g("volume", "Volume", "v") if ("volume" in row or "Volume" in row or "v" in row) else 0.0,
            ))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="run on synthetic data")
    ap.add_argument("--csv", help="backtest a CSV of candles")
    ap.add_argument("--direction", default="long_only", choices=["long_only", "long_short"])
    args = ap.parse_args()

    cfg = Config(direction=args.direction)
    if args.csv:
        candles = load_csv(args.csv)
        src = args.csv
    else:
        candles = synth_candles()
        src = "synthetic demo data"

    stats = backtest(candles, cfg)
    print(f"Sneaky Pivot backtest — {src}  ({len(candles)} candles, {cfg.direction})")
    print("-" * 56)
    print(f"  trades          : {stats['trades']}")
    print(f"  win rate        : {stats['win_rate']:.1f}%")
    print(f"  total R         : {stats['total_R']:+.2f}")
    print(f"  avg R / trade   : {stats['avg_R']:+.3f}")
    pf = stats['profit_factor']
    print(f"  profit factor   : {'∞' if pf==float('inf') else f'{pf:.2f}'}")
    print(f"  max drawdown R  : {stats['max_drawdown_R']:.2f}")
    print("-" * 56)
    print("NOTE: synthetic data is for wiring/sanity only — NOT an edge claim.")
    print("Real validation requires real 15m candles for the symbols you'll trade.")


if __name__ == "__main__":
    main()
