"""
dashboard_strategies.py  —  drop-in Strategy Committee dashboard
================================================================

Adds two routes WITHOUT touching bot_with_proxy.py:
    GET /strategies       -> the HTML dashboard
    GET /api/strategies   -> JSON the dashboard polls every 30s

Wire it into your existing Flask app with ONE line (after `app = Flask(...)`):

    from dashboard_strategies import strategy_dashboard, set_ohlcv_provider
    app.register_blueprint(strategy_dashboard)
    set_ohlcv_provider(my_fetch_ohlcv)   # <- your existing candle fetcher

`my_fetch_ohlcv(symbol, timeframe="1h", limit=300)` must return a pandas
DataFrame with columns: open, high, low, close, volume (oldest first).
That's the only integration point. Until you wire it, the API returns a
clearly-labelled empty payload and the page falls back to its sample view.

The regime classifier here is intentionally lightweight — it gets replaced by
the real Committee module in Batch 3, which will also produce the execution
decision. For now it just drives the matrix highlight.
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, send_file

from strategy_library import BATCH_1, atr, ema
from strategy_library_batch2 import BATCH_2, PAIR_STRATEGY

strategy_dashboard = Blueprint("strategy_dashboard", __name__)

# all single-asset strategies the dashboard evaluates
SINGLE = BATCH_1 + BATCH_2

# symbols to scan + the equity/capital readout (override via env or your loop)
SYMBOLS = os.getenv("DASH_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT").split(",")
PAIR = ("BTC/USDT", "ETH/USDT")

_HTML = os.path.join(os.path.dirname(__file__), "strategy_dashboard.html")

# pluggable data source — set via set_ohlcv_provider()
_ohlcv_provider: Optional[Callable[..., pd.DataFrame]] = None


def set_ohlcv_provider(fn: Callable[..., pd.DataFrame]) -> None:
    """Register your existing candle fetcher. Signature:
       fn(symbol, timeframe='1h', limit=300) -> DataFrame[open,high,low,close,volume]"""
    global _ohlcv_provider
    _ohlcv_provider = fn


def set_account(equity: float, capital: float) -> None:
    """Optional: push live account numbers from your loop."""
    global _equity, _capital
    _equity, _capital = float(equity), float(capital)


_equity = float(os.getenv("DASH_EQUITY", "53.40"))
_capital = float(os.getenv("DASH_CAPITAL", "24.11"))


# ---------------------------------------------------------------------------
# Lightweight regime classifier (placeholder until the Batch-3 Committee)
# ---------------------------------------------------------------------------
def _adx(df: pd.DataFrame, length: int = 14) -> float:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, length)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/length, adjust=False).mean() / tr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/length, adjust=False).mean() / tr
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return float(dx.ewm(alpha=1/length, adjust=False).mean().iloc[-1])


def classify_regime(df: pd.DataFrame) -> str:
    """Strong Trend | Ranging | High Volatility (cheap 3-way for the matrix)."""
    if df is None or len(df) < 60:
        return "Ranging"
    a = atr(df, 14)
    atr_pct = float(a.iloc[-1] / df["close"].iloc[-1])
    atr_hist = (a / df["close"]).rolling(100).rank(pct=True).iloc[-1]
    try:
        adx = _adx(df)
    except Exception:
        adx = 0.0
    # high realized vol dominates the label
    if not np.isnan(atr_hist) and atr_hist > 0.85:
        return "High Volatility"
    return "Strong Trend" if adx >= 25 else "Ranging"


# ---------------------------------------------------------------------------
# Build the payload
# ---------------------------------------------------------------------------
def _empty(reason: str) -> dict:
    return {"equity": _equity, "capital": _capital,
            "updated": datetime.now(timezone.utc).isoformat(),
            "data_source": "sample", "note": reason,
            "regime": {"label": "Ranging"}, "signals": []}


def build_payload() -> dict:
    if _ohlcv_provider is None:
        return _empty("no ohlcv provider registered — call set_ohlcv_provider()")

    signals, regime_votes = [], []
    for sym in SYMBOLS:
        try:
            df = _ohlcv_provider(sym, timeframe="1h", limit=300)
        except Exception as e:
            signals.append({"strategy": "feed", "symbol": sym, "direction": "flat",
                            "fired": False, "reason": f"fetch failed: {type(e).__name__}"})
            continue
        regime_votes.append(classify_regime(df))
        for strat in SINGLE:
            try:
                sig = strat.evaluate(df, sym, _equity, capital_available=_capital)
            except Exception as e:
                sig = None
            if sig:
                signals.append(sig.as_dict() | {"fired": True})

    # pair strategy (two legs)
    try:
        da = _ohlcv_provider(PAIR[0], timeframe="1h", limit=300)
        db = _ohlcv_provider(PAIR[1], timeframe="1h", limit=300)
        for leg in PAIR_STRATEGY.evaluate_pair(da, db, PAIR[0], PAIR[1], _equity, _capital):
            signals.append(leg.as_dict() | {"fired": True})
    except Exception:
        pass

    # majority regime across scanned symbols
    label = max(set(regime_votes), key=regime_votes.count) if regime_votes else "Ranging"
    return {"equity": _equity, "capital": _capital,
            "updated": datetime.now(timezone.utc).isoformat(),
            "data_source": "live", "regime": {"label": label}, "signals": signals}


@strategy_dashboard.route("/api/strategies")
def api_strategies():
    return jsonify(build_payload())


@strategy_dashboard.route("/strategies")
def strategies_page():
    return send_file(_HTML)
