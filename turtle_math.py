"""
turtle_math.py — Turtle Trading System math primitives.

Self-contained module: takes bars, returns signals. No external deps.
Imported by bot_with_proxy.py (stocks) and binance_crypto.py (crypto).

Faithful to Dennis/Faith original Turtle rules:
  • System 1: enter on 20-day breakout, exit on 10-day breakdown
  • System 2: enter on 55-day breakout, exit on 20-day breakdown
  • Stop = entry - (2 × N), where N = 20-period ATR
  • Position size = 1% of equity ÷ (2N × dollar_per_point)

Bar format expected: list of dicts with keys 'o', 'h', 'l', 'c', 'v'.
The newest bar is at the END of the list (bars[-1]).
"""
from typing import Optional


def compute_donchian(bars: list) -> Optional[dict]:
    """
    Compute Donchian channels — the breakout signal for Turtle Trading.
    Returns highest high and lowest low over 20 and 55 prior bars
    (excluding current bar), plus breakout flags.

    Args:
        bars: list of dicts with 'h', 'l', 'c'. Newest bar last.

    Returns dict with high_20, low_20, high_55, low_55, current_close,
    breakout_up_20, breakout_down_20, breakout_up_55, breakout_down_55.
    Returns None if not enough history (needs ≥56 bars for high_55).
    """
    if not bars or len(bars) < 56:
        return None
    # Exclude current bar — Turtle compares today's close to prior N days
    prior = bars[:-1]
    current_close = bars[-1]["c"]

    high_20 = max(b["h"] for b in prior[-20:])
    low_20  = min(b["l"] for b in prior[-20:])
    high_55 = max(b["h"] for b in prior[-55:])
    low_55  = min(b["l"] for b in prior[-55:])

    return {
        "high_20":          round(high_20, 6),
        "low_20":           round(low_20, 6),
        "high_55":          round(high_55, 6),
        "low_55":           round(low_55, 6),
        "current_close":    round(current_close, 6),
        "breakout_up_20":   current_close > high_20,
        "breakout_down_20": current_close < low_20,
        "breakout_up_55":   current_close > high_55,
        "breakout_down_55": current_close < low_55,
    }


def compute_atr(bars: list, period: int = 20) -> Optional[float]:
    """
    Average True Range — the volatility unit 'N' in Turtle terminology.
    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = simple moving average of True Range over `period` bars.

    Args:
        bars: list of dicts with 'h', 'l', 'c'. Newest bar last.
        period: ATR lookback (default 20, the Turtle standard).

    Returns the ATR value, or None if insufficient bars.
    """
    if not bars or len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["h"]
        l = bars[i]["l"]
        prev_c = bars[i - 1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    # Take the last `period` true ranges
    if len(trs) < period:
        return None
    recent = trs[-period:]
    return round(sum(recent) / len(recent), 6)


def compute_turtle_signal(bars: list, system: int = 1) -> Optional[dict]:
    """
    Compute Turtle System 1 (20/10) or System 2 (55/20) entry signal.

    Args:
        bars: list of OHLCV dicts (newest last).
        system: 1 for 20/10, 2 for 55/20.

    Returns dict with entry_signal, entry_level, stop_price, atr, or None.
    """
    if system not in (1, 2):
        system = 1
    donch = compute_donchian(bars)
    if donch is None:
        return None
    atr = compute_atr(bars, period=20)   # Turtle always uses 20-period ATR
    if atr is None or atr <= 0:
        return None

    current_close = donch["current_close"]
    if system == 1:
        entry_signal = donch["breakout_up_20"]
        entry_level  = donch["high_20"]
    else:  # system 2
        entry_signal = donch["breakout_up_55"]
        entry_level  = donch["high_55"]

    # Stop is 2N below entry — Turtle's only stop rule
    stop_price = round(current_close - (2 * atr), 6)

    return {
        "entry_signal": bool(entry_signal),
        "entry_level":  round(entry_level, 6),
        "current_close": round(current_close, 6),
        "stop_price":   stop_price,
        "atr":          round(atr, 6),
        "system":       system,
    }


def compute_turtle_position_size(
    account_equity: float,
    atr: float,
    current_price: float,
    dollar_per_point: float = 1.0,
) -> dict:
    """
    Turtle position sizing: risk 1% of equity per trade per unit.
    One 'unit' = (1% of equity) / (N × dollar_per_point).
    The 2N stop means each unit risks 1% of equity at stop-out.

    Args:
        account_equity:    total account value
        atr:               20-period ATR (the 'N')
        current_price:     current price (entry price assumed ≈ close)
        dollar_per_point:  $ change per 1.0 price move. 1.0 for stocks/crypto.

    Returns dict with units (qty), notional_usd, risk_usd, risk_pct.
    """
    if atr is None or atr <= 0 or account_equity <= 0 or current_price <= 0:
        return {"units": 0, "notional_usd": 0, "risk_usd": 0, "risk_pct": 0}

    risk_usd     = account_equity * 0.01           # 1% of equity
    units        = risk_usd / (atr * dollar_per_point * 2)   # 2N stop
    # Cap at not buying more than equity (paranoid guard)
    notional_usd = round(units * current_price * dollar_per_point, 2)
    if notional_usd > account_equity:
        notional_usd = round(account_equity, 2)
        units        = notional_usd / (current_price * dollar_per_point)

    actual_risk_usd = round(units * atr * dollar_per_point * 2, 4)
    risk_pct        = round((actual_risk_usd / account_equity) * 100, 2) if account_equity else 0

    return {
        "units":        round(units, 6),
        "notional_usd": notional_usd,
        "risk_usd":     actual_risk_usd,
        "risk_pct":     risk_pct,
    }


def should_turtle_exit(
    bars: list,
    entry_price: float,
    atr_at_entry: float,
    system: int = 1,
) -> dict:
    """
    Check if a Turtle position should be exited.
    Exit conditions (any one triggers):
      1. Price hit 2N stop below entry
      2. Price broke below the 10-day low (System 1) or 20-day low (System 2)

    Args:
        bars:         current bars (newest last)
        entry_price:  original entry price
        atr_at_entry: ATR (N) at the time of entry
        system:       1 (10-day exit) or 2 (20-day exit)

    Returns dict with should_exit (bool), reason (str), exit_level (float).
    """
    if not bars or atr_at_entry is None or atr_at_entry <= 0:
        return {"should_exit": False, "reason": "insufficient data", "exit_level": None}

    current_close = bars[-1]["c"]
    current_low   = bars[-1]["l"]

    # 1. 2N stop check
    stop_price = entry_price - (2 * atr_at_entry)
    if current_low <= stop_price:
        return {
            "should_exit": True,
            "reason":      f"2N stop hit (${stop_price:.4f})",
            "exit_level":  round(stop_price, 6),
        }

    # 2. Donchian exit check
    exit_period = 10 if system == 1 else 20
    if len(bars) < exit_period + 1:
        return {"should_exit": False, "reason": "no exit signal", "exit_level": None}
    prior = bars[:-1]
    exit_low = min(b["l"] for b in prior[-exit_period:])
    if current_close < exit_low:
        return {
            "should_exit": True,
            "reason":      f"{exit_period}-day breakdown (${exit_low:.4f})",
            "exit_level":  round(exit_low, 6),
        }

    return {"should_exit": False, "reason": "no exit signal", "exit_level": None}
