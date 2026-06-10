"""
committee_shadow.py  —  Shadow-mode Strategy Committee
======================================================

Runs the Regime Detector + Committee against live candles on a background
thread and LOGS what it would have done. It places no orders, holds no
positions, and touches no broker. Its entire job is to build an evidence
trail you can compare against what Grok/Claude actually did, so the
Committee has to EARN execution authority with data.

Wiring (3 lines in bot_with_proxy.py, next to the Flask app):

    import committee_shadow
    committee_shadow.start_shadow_thread()           # background, daemon
    app.add_url_rule("/shadow", "shadow", committee_shadow.shadow_endpoint)

Output, every cycle, per symbol:
    [SHADOW] BTCUSDT  regime=ranging  -> stand_aside (score=+0.42, agree=1)
plus a JSON line appended to SHADOW_LOG_PATH (survives within a deploy;
Railway's disk is ephemeral across deploys, so the /shadow endpoint is the
durable view — scrape it or eyeball it).

Env knobs:
    SHADOW_SYMBOLS   comma list, default "BTCUSDT,ETHUSDT,SOLUSDT,LINKUSDT,NEARUSDT"
    SHADOW_INTERVAL  seconds between cycles, default 900 (15 min)
    SHADOW_EQUITY    equity used for sizing math, default 52.0
"""

from __future__ import annotations
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone

import pandas as pd

from committee import Committee, RegimeDetector

SHADOW_LOG_PATH = os.environ.get("SHADOW_LOG_PATH", "/tmp/shadow_decisions.jsonl")
SYMBOLS  = [s.strip() for s in os.environ.get(
    "SHADOW_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,LINKUSDT,NEARUSDT").split(",") if s.strip()]
INTERVAL = int(os.environ.get("SHADOW_INTERVAL", "900"))
EQUITY   = float(os.environ.get("SHADOW_EQUITY", "52.0"))

_recent: deque = deque(maxlen=400)      # in-memory ring for /shadow endpoint
_thread: threading.Thread | None = None
_started = False
_stats = {"cycles": 0, "decisions": 0, "acts": 0, "stand_asides": 0,
          "errors": 0, "started_at": None, "last_cycle_at": None}


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SHADOW] {msg}", flush=True)


def _bars_to_df(bars: list) -> pd.DataFrame:
    """Convert binance_crypto.get_crypto_bars() output ({t,o,h,l,c,v} dicts)
    into the OHLCV DataFrame the strategy library expects."""
    df = pd.DataFrame(bars).rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    if "t" in df.columns:
        df["t"] = pd.to_datetime(df["t"])
        df = df.set_index("t")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _one_cycle(committee: Committee) -> None:
    import binance_crypto  # local import: module must run inside the bot env

    for sym in SYMBOLS:
        try:
            bars = binance_crypto.get_crypto_bars(sym, interval="1h", limit=300)
            if not bars or len(bars) < 60:
                _log(f"{sym}: insufficient bars ({len(bars) if bars else 0}) — skipped")
                continue
            df = _bars_to_df(bars)
            d = committee.decide(df, sym, EQUITY, capital_available=EQUITY * 0.45)

            record = d.as_dict() | {
                "ts": datetime.now(timezone.utc).isoformat(),
                "close": float(df["close"].iloc[-1]),
            }
            _recent.append(record)
            _stats["decisions"] += 1
            if d.action == "stand_aside":
                _stats["stand_asides"] += 1
                _log(f"{sym}  regime={d.regime}  -> stand_aside "
                     f"(score={d.score:+.2f}, agree={d.n_agree})"
                     + (f" voices={d.contributors}" if d.contributors else ""))
            else:
                _stats["acts"] += 1
                _log(f"{sym}  regime={d.regime}  -> {d.action.upper()} "
                     f"score={d.score:+.2f} agree={d.n_agree} "
                     f"entry={d.entry} stop={d.stop} tp={d.take_profit} "
                     f"size={d.size}  [{d.rationale}]")

            try:
                with open(SHADOW_LOG_PATH, "a") as f:
                    f.write(json.dumps(record) + "\n")
            except Exception:
                pass    # ephemeral disk problems must never matter

        except Exception as e:
            _stats["errors"] += 1
            _log(f"{sym}: cycle error {type(e).__name__}: {e}")


def _loop() -> None:
    committee = Committee(RegimeDetector())
    _log(f"shadow committee online — symbols={SYMBOLS} every {INTERVAL}s "
         f"(signal-only, no orders ever)")
    while True:
        try:
            _one_cycle(committee)
            _stats["cycles"] += 1
            _stats["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            _stats["errors"] += 1
            _log("cycle crashed:\n" + traceback.format_exc()[-400:])
        time.sleep(INTERVAL)


def start_shadow_thread() -> bool:
    """Start the background shadow loop (idempotent, daemon thread)."""
    global _thread, _started
    if _started:
        return False
    _started = True
    _stats["started_at"] = datetime.now(timezone.utc).isoformat()
    _thread = threading.Thread(target=_loop, name="committee-shadow", daemon=True)
    _thread.start()
    return True


def shadow_endpoint():
    """Flask view: GET /shadow -> stats + recent decisions (newest first)."""
    from flask import jsonify
    return jsonify({
        "mode": "shadow (signal-only — no orders are placed)",
        "stats": _stats,
        "symbols": SYMBOLS,
        "interval_sec": INTERVAL,
        "recent": list(_recent)[::-1][:100],
    })
