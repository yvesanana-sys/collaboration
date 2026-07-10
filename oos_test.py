#!/usr/bin/env python3
"""
oos_test.py — the honest test. For each coin:
  1. split its 15m history into TRAIN (first 60%) and TEST (last 40%),
  2. tune parameters on TRAIN only (small grid),
  3. apply those tuned params to the TEST half it has NEVER seen,
  4. report TRAIN vs TEST.

Why this matters: any strategy can be made to look good on data you tuned it on.
The only thing that counts is whether that edge survives on unseen data. If TRAIN
looks great but TEST collapses, the "edge" was luck / overfitting.

The number to watch is the aggregate TEST R across the whole basket — NOT any
single coin. One coin winning is noise; the basket surviving out-of-sample is
signal.

Needs sneaky_pivot.py in the same folder.

    python oos_test.py
    python oos_test.py --symbols INJ SOL TAO FIL --candles 10000
"""
import argparse
import itertools
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    import sneaky_pivot as sp
except ImportError:
    sys.exit("sneaky_pivot.py must be in the same folder as this script.")

BINANCE_US = "https://api.binance.us/api/v3/klines"

# AI / infra / DePIN-themed, volatile — the category the hunch was about.
DEFAULT_SYMBOLS = ["INJ", "SOL", "FET", "RENDER", "NEAR", "GRT", "TAO", "FIL",
                   "AR", "THETA", "ICP", "RUNE", "TIA", "SUI", "SEI", "AVAX",
                   "DOT", "LINK"]

# Small grid on purpose — a big grid just overfits harder.
GRID_ORC_MULT = [1.5, 1.8, 2.2]
GRID_STOP_ATR = [0.20, 0.30]
MIN_TRADES = 8            # ignore fits/tests with too few trades to mean anything


def fetch_klines(symbol_usdt, interval="15m", total=8000):
    out, end_time = [], None
    while len(out) < total:
        params = {"symbol": symbol_usdt, "interval": interval, "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time
        r = requests.get(BINANCE_US, params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out = batch + out
        end_time = int(batch[0][0]) - 1
        if len(batch) < 1000:
            break
        time.sleep(0.25)
    out = out[-total:]
    return [sp.Candle(ts=int(k[0]), open=float(k[1]), high=float(k[2]),
                      low=float(k[3]), close=float(k[4]), volume=float(k[5]))
            for k in out]


def tune_on_train(train, direction):
    """Pick the param set with the best TRAIN total_R (with a min-trades guard)."""
    best = None
    for om, sa in itertools.product(GRID_ORC_MULT, GRID_STOP_ATR):
        cfg = sp.Config(orc_mult=om, stop_atr=sa, direction=direction)
        s = sp.backtest(train, cfg)
        if s["trades"] < MIN_TRADES:
            continue
        if best is None or s["total_R"] > best[0]:
            best = (s["total_R"], cfg, s)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--candles", type=int, default=8000, help="≈83 days at 8000")
    ap.add_argument("--direction", default="long_only", choices=["long_only", "long_short"])
    args = ap.parse_args()

    print(f"Out-of-sample test — tune on first 60%, measure on unseen last 40%.")
    print(f"Basket: {len(args.symbols)} coins · {args.candles} candles each · {args.direction}\n")

    results = []
    for base in args.symbols:
        pair = f"{base.upper()}USDT"
        try:
            candles = fetch_klines(pair, total=args.candles)
        except Exception as e:
            print(f"  {base:<7} skipped (fetch failed / not listed)")
            continue
        if len(candles) < 500:
            print(f"  {base:<7} skipped (only {len(candles)} candles)")
            continue
        split = int(len(candles) * 0.60)
        train, test = candles[:split], candles[split:]
        best = tune_on_train(train, args.direction)
        if not best:
            print(f"  {base:<7} skipped (not enough train trades)")
            continue
        train_R, cfg, train_stats = best
        test_stats = sp.backtest(test, cfg)
        if test_stats["trades"] < MIN_TRADES:
            print(f"  {base:<7} skipped (only {test_stats['trades']} test trades)")
            continue
        results.append((base.upper(), cfg, train_stats, test_stats))
        print(f"  {base:<7} ok  (tuned orc_mult={cfg.orc_mult}, stop_atr={cfg.stop_atr})")

    if not results:
        print("\nNo usable results.")
        return

    print("\n" + "=" * 78)
    print(f"{'SYMBOL':<8}{'TRAIN R':>10}{'TEST TR':>9}{'TEST R':>10}{'TEST PF':>9}"
          f"{'TEST WIN%':>11}{'  HOLDS?':>9}")
    print("-" * 78)
    agg_train = agg_test = 0.0
    held = 0
    for sym, cfg, tr, te in results:
        pf = te["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        holds = te["total_R"] > 0
        held += 1 if holds else 0
        agg_train += tr["total_R"]
        agg_test += te["total_R"]
        print(f"{sym:<8}{tr['total_R']:>+10.2f}{te['trades']:>9}{te['total_R']:>+10.2f}"
              f"{pf_s:>9}{te['win_rate']:>10.1f}%{'  YES' if holds else '   no':>9}")
    print("-" * 78)
    print(f"{'BASKET':<8}{agg_train:>+10.2f}{'':>9}{agg_test:>+10.2f}")
    print("=" * 78)
    n = len(results)
    print(f"\nCoins that stayed profitable out-of-sample: {held}/{n}")
    print(f"Aggregate TRAIN R: {agg_train:+.2f}   |   Aggregate TEST R: {agg_test:+.2f}")
    print("\nVerdict guide:")
    print("  • TRAIN strongly +, TEST ~0 or negative  → overfitting. No real edge. Stop.")
    print("  • TEST + across the BASKET and majority of coins hold → worth deeper work.")
    print("  • A high TRAIN and a collapsed TEST is the classic luck signature.")
    if agg_test <= 0 or held < n * 0.6:
        print("\n>>> Reading: the edge did NOT survive out-of-sample. This is the")
        print(">>> definitive 'it was luck' result we were testing for.")
    else:
        print("\n>>> Reading: it held up out-of-sample — unusual and worth a rigorous")
        print(">>> follow-up before trusting it (more coins, more history, costs).")


if __name__ == "__main__":
    main()
