#!/usr/bin/env python3
"""
fetch_and_test.py — pull REAL 15-minute candles from Binance.US (no API keys
needed) and run the Sneaky Pivot backtest on each. This is the "does it have a
real edge?" test, on actual market data.

Requires sneaky_pivot.py in the SAME folder.

Run (defaults to BTC, ETH, SOL, XRP):
    python fetch_and_test.py
Pick your own + more history:
    python fetch_and_test.py --symbols BTC ETH SOL DOGE --candles 8000
    python fetch_and_test.py --direction long_short

Each symbol's candles are also saved to <SYMBOL>_15m.csv, so you can re-run
    python sneaky_pivot.py --csv BTC_15m.csv
later without re-downloading.
"""
import argparse
import csv
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Please install requests:  pip install requests")

try:
    import sneaky_pivot as sp
except ImportError:
    sys.exit("sneaky_pivot.py must be in the same folder as this script "
             "(upload it alongside this file).")

BINANCE_US = "https://api.binance.us/api/v3/klines"


def fetch_klines(symbol_usdt: str, interval: str = "15m", total: int = 5000):
    """Paginate Binance.US klines backward until we have `total` candles."""
    out = []
    end_time = None
    while len(out) < total:
        params = {"symbol": symbol_usdt, "interval": interval, "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time
        r = requests.get(BINANCE_US, params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out = batch + out                      # each batch is older; prepend
        end_time = int(batch[0][0]) - 1        # step back before earliest openTime
        if len(batch) < 1000:
            break
        time.sleep(0.25)                       # be polite to the API
    out = out[-total:]
    candles = [sp.Candle(ts=int(k[0]), open=float(k[1]), high=float(k[2]),
                         low=float(k[3]), close=float(k[4]), volume=float(k[5]))
               for k in out]
    return candles


def save_csv(symbol: str, candles):
    path = f"{symbol}_15m.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL", "XRP"],
                    help="base symbols; USDT is appended automatically")
    ap.add_argument("--candles", type=int, default=5000,
                    help="how many 15m candles per symbol (5000 ≈ 52 days)")
    ap.add_argument("--direction", default="long_only",
                    choices=["long_only", "long_short"])
    args = ap.parse_args()

    cfg = sp.Config(direction=args.direction)
    rows = []
    print(f"Fetching {args.candles} × 15m candles per symbol from Binance.US "
          f"(no keys)…  direction={args.direction}\n")

    for base in args.symbols:
        pair = f"{base.upper()}USDT"
        try:
            candles = fetch_klines(pair, total=args.candles)
        except Exception as e:
            print(f"  {base:<5} — fetch failed: {e}")
            continue
        if len(candles) < cfg.orc_lookback + 5:
            print(f"  {base:<5} — not enough candles ({len(candles)})")
            continue
        path = save_csv(base.upper(), candles)
        stats = sp.backtest(candles, cfg)
        rows.append((base.upper(), len(candles), stats))
        print(f"  {base:<5} — {len(candles)} candles → saved {path}")

    if not rows:
        print("\nNo data fetched. If you're on a network that blocks Binance.US, "
              "try again from Colab or another connection.")
        return

    # Comparison table
    print("\n" + "=" * 72)
    print(f"{'SYMBOL':<7}{'CANDLES':>9}{'TRADES':>8}{'WIN%':>7}{'TOTAL R':>10}"
          f"{'AVG R':>8}{'PF':>7}{'MAXDD R':>9}")
    print("-" * 72)
    agg_R = 0.0
    agg_trades = 0
    for sym, n, s in rows:
        pf = s["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{sym:<7}{n:>9}{s['trades']:>8}{s['win_rate']:>6.1f}%"
              f"{s['total_R']:>+10.2f}{s['avg_R']:>+8.3f}{pf_s:>7}{s['max_drawdown_R']:>9.2f}")
        agg_R += s["total_R"]
        agg_trades += s["trades"]
    print("-" * 72)
    print(f"{'ALL':<7}{'':<9}{agg_trades:>8}{'':>7}{agg_R:>+10.2f}")
    print("=" * 72)
    print("\nHow to read this:")
    print("  • TOTAL R > 0 and PF > 1.0 across several symbols = a real signal worth wiring.")
    print("  • Near 0 / PF ~1 = no edge yet — we tune parameters before touching the bot.")
    print("  • One good symbol isn't enough; look for consistency across all of them.")
    print("  • This is in-sample on recent data — treat as a first look, not proof.")


if __name__ == "__main__":
    main()
