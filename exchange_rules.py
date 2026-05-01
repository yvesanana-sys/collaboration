"""
exchange_rules.py — Per-coin Binance.US trading rules cache.

═══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
═══════════════════════════════════════════════════════════════════════
Binance.US has different MIN_NOTIONAL, tick_size, and step_size values
for each trading pair. The bot's old behavior hardcoded $10 for every
coin, which:
  1. Sometimes blocked valid sub-$10 trades on cheap coins
  2. Sometimes proposed sub-$10 trades on coins that DO require $10
  3. Re-fetched filters from Binance on every trade (no persistence)

This module solves all three by:
  - Pre-scanning the universe on boot (one batched API call)
  - Persisting per-coin rules to /data/exchange_rules.json
  - Using exchange's actual minimum, BUT clamping at the bot's own $5 floor
    (because $5 is the smallest trade that nets meaningful profit after fees)

═══════════════════════════════════════════════════════════════════════
EFFECTIVE MIN LOGIC
═══════════════════════════════════════════════════════════════════════
For any trade:
    effective_min = max(BOT_GLOBAL_FLOOR, exchange_per_coin_min)

So:
  - Coin requires $10 on Binance, bot floor $5 → use $10 (exchange wins)
  - Coin requires $3 on Binance, bot floor $5 → use $5 (bot floor wins)
  - Unknown coin (cache miss) → use $10 safe fallback until first fetch

═══════════════════════════════════════════════════════════════════════
PERSISTENCE
═══════════════════════════════════════════════════════════════════════
Cache lives at /data/exchange_rules.json. Format:
  {
    "binance_us": {
      "BTCUSDT": {
        "min_notional":  10.0,
        "tick_size":     0.01,
        "step_size":     0.00001,
        "status":        "TRADING",
        "last_fetched":  "2026-05-01T12:34:56Z"
      },
      ...
    },
    "global_floor":      5.0,
    "fallback_min":      10.0,
    "last_full_scan":    "2026-05-01T12:30:00Z",
    "scan_count":        17
  }

Cache freshness: 24 hours. After that, next get_min_notional triggers
a refresh. If exchange call fails, cached value is used regardless of age.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

# ── Configuration ───────────────────────────────────────────
BOT_GLOBAL_FLOOR_USDT = 5.0     # Bot's own minimum — won't propose smaller trades.
                                 # Below this, fees eat profit margin.
SAFE_FALLBACK_MIN_USDT = 10.0    # Used when exchange filter unknown.
                                 # Binance.US standard for most major USDT pairs.

CACHE_TTL_HOURS = 24             # Rules rarely change; refresh once a day.
STATE_FILE = "/data/exchange_rules.json"
FALLBACK_STATE_FILE = "./exchange_rules.json"

# ── Injected dependencies ───────────────────────────────────
log = print
binance_get = None       # Set via _set_context()


def _set_context(log_fn=None, binance_get_fn=None):
    """Wire in the bot's logger and Binance API helper."""
    global log, binance_get
    if log_fn:           log = log_fn
    if binance_get_fn:   binance_get = binance_get_fn


# ── State management ────────────────────────────────────────
_state = None    # Lazy-loaded cache


def _default_state() -> dict:
    return {
        "binance_us":      {},
        "global_floor":    BOT_GLOBAL_FLOOR_USDT,
        "fallback_min":    SAFE_FALLBACK_MIN_USDT,
        "last_full_scan":  None,
        "scan_count":      0,
    }


def _load_state() -> dict:
    """Load cache from /data/ — survives redeploys."""
    global _state
    if _state is not None:
        return _state
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path) as f:
                _state = json.load(f)
                # Ensure keys exist (handles schema additions over time)
                defaults = _default_state()
                for k, v in defaults.items():
                    if k not in _state:
                        _state[k] = v
                return _state
        except FileNotFoundError:
            continue
        except Exception as e:
            try: log(f"⚠️ exchange_rules load failed at {path}: {e}")
            except Exception: pass
    _state = _default_state()
    return _state


def _save_state() -> bool:
    """Persist cache to /data/."""
    global _state
    if _state is None:
        return False
    for path in [STATE_FILE, FALLBACK_STATE_FILE]:
        try:
            with open(path, "w") as f:
                json.dump(_state, f, indent=2, default=str)
            return True
        except Exception:
            continue
    return False


# ── Freshness check ─────────────────────────────────────────
def _is_fresh(symbol: str) -> bool:
    """True if we have a cached entry for symbol that's < 24h old."""
    s = _load_state()
    entry = s["binance_us"].get(symbol)
    if not entry or not entry.get("last_fetched"):
        return False
    try:
        fetched_at = datetime.fromisoformat(entry["last_fetched"].replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched_at
        return age < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


# ── Single-symbol fetch ─────────────────────────────────────
def _fetch_symbol_from_exchange(symbol: str) -> dict:
    """
    Fetch one symbol's filters from Binance.US /api/v3/exchangeInfo.
    Returns dict with min_notional, tick_size, step_size, status.
    Raises on failure (caller decides fallback).
    """
    if not binance_get:
        raise RuntimeError("binance_get not wired — call _set_context first")
    data = binance_get("/api/v3/exchangeInfo", {"symbol": symbol})
    symbols = data.get("symbols", [])
    if not symbols:
        raise ValueError(f"No symbol info returned for {symbol}")
    info = symbols[0]
    rule = {
        "min_notional": SAFE_FALLBACK_MIN_USDT,
        "tick_size":    0.00000001,
        "step_size":    1.0,
        "status":       info.get("status", "UNKNOWN"),
        "last_fetched": datetime.now(timezone.utc).isoformat(),
    }
    for f in info.get("filters", []):
        ftype = f.get("filterType", "")
        if ftype == "PRICE_FILTER":
            rule["tick_size"] = float(f.get("tickSize", rule["tick_size"]))
        elif ftype == "LOT_SIZE":
            rule["step_size"] = float(f.get("stepSize", rule["step_size"]))
        elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
            rule["min_notional"] = float(
                f.get("minNotional", f.get("minQty", SAFE_FALLBACK_MIN_USDT))
            )
    return rule


# ── Public API ──────────────────────────────────────────────
def get_rule(symbol: str, force_refresh: bool = False) -> dict:
    """
    Returns the full rule dict for a symbol.
    Lazy-fetches if cache miss or stale (24h+).
    On API failure with no cache: returns safe-fallback dict.
    """
    s = _load_state()
    symbol = symbol.upper()

    if not force_refresh and _is_fresh(symbol):
        return s["binance_us"][symbol]

    # Cache miss or stale — try to refresh
    try:
        rule = _fetch_symbol_from_exchange(symbol)
        s["binance_us"][symbol] = rule
        _save_state()
        log(f"📋 Cached rule for {symbol}: min=${rule['min_notional']:.2f} "
            f"tick={rule['tick_size']} step={rule['step_size']} "
            f"status={rule['status']}")
        return rule
    except Exception as e:
        # If we have a stale cached value, return it rather than failing
        if symbol in s["binance_us"]:
            log(f"⚠️ Refresh failed for {symbol} ({e}) — using stale cache")
            return s["binance_us"][symbol]
        # No cache — return safe fallback (DON'T persist; we don't actually know)
        log(f"⚠️ No rule cached for {symbol} and fetch failed ({e}) — "
            f"using fallback ${SAFE_FALLBACK_MIN_USDT:.2f}")
        return {
            "min_notional": SAFE_FALLBACK_MIN_USDT,
            "tick_size":    0.00000001,
            "step_size":    1.0,
            "status":       "UNKNOWN",
            "last_fetched": None,
        }


def get_min_notional(symbol: str) -> float:
    """Just the exchange's minimum for a symbol (without bot floor applied)."""
    return get_rule(symbol).get("min_notional", SAFE_FALLBACK_MIN_USDT)


def get_effective_min(symbol: str) -> float:
    """
    Effective minimum the bot will actually use for this symbol.
    Takes the LARGER of:
      - Bot's global floor ($5 by default)
      - Exchange's actual per-coin minimum

    Exchange always wins when its min is higher. Bot floor wins when
    exchange allows trades smaller than $5 (we don't bother — fees eat profit).
    """
    s = _load_state()
    bot_floor = s.get("global_floor", BOT_GLOBAL_FLOOR_USDT)
    exchange_min = get_min_notional(symbol)
    return max(bot_floor, exchange_min)


def is_tradeable(symbol: str) -> bool:
    """True if symbol is in TRADING status on the exchange."""
    return get_rule(symbol).get("status") == "TRADING"


def scan_universe(symbols: list) -> dict:
    """
    Pre-scan a list of symbols in one batched call to /api/v3/exchangeInfo.
    Faster than individual fetches; should be called on boot for the universe.
    Returns: dict of {symbol: rule} for all successfully fetched.
    """
    if not binance_get:
        log("⚠️ scan_universe skipped — binance_get not wired")
        return {}
    s = _load_state()
    results = {}
    fetched = 0
    failed  = 0
    try:
        # Binance allows fetching multiple symbols in one call by passing
        # symbols=["A","B"] as a JSON array string. But /api/v3/exchangeInfo
        # without args returns ALL symbols (~1500 — too much). Best balance:
        # one call per symbol, but rate-limit-aware.
        # Note: scan only happens once per 24h so this is fine.
        for sym in symbols:
            sym = sym.upper()
            try:
                rule = _fetch_symbol_from_exchange(sym)
                s["binance_us"][sym] = rule
                results[sym] = rule
                fetched += 1
            except Exception as e:
                failed += 1
                # Don't log every individual failure — keep the boot log readable
                continue
        s["last_full_scan"] = datetime.now(timezone.utc).isoformat()
        s["scan_count"]     = s.get("scan_count", 0) + 1
        _save_state()
        log(f"📋 Exchange rules scan complete: {fetched} symbols cached, "
            f"{failed} failed (cached: {len(s['binance_us'])} total)")
    except Exception as e:
        log(f"⚠️ scan_universe error: {e}")
    return results


def format_for_ai_prompt(symbols: list = None, max_lines: int = 5) -> str:
    """
    Compact one-line summary of exchange minimums for AI prompts.
    If most symbols share a min (typical $10), shows aggregate + exceptions.

    Returns a string suitable for prompt injection. Examples:
      "Min order: $10 across all listed coins"
      "Min order: $10 (FETUSDT $5, KAVAUSDT $5)"
      "Min order: $10 (no per-coin overrides cached)"
    """
    s = _load_state()
    cache = s.get("binance_us", {})
    if not cache:
        return f"Min order: ${SAFE_FALLBACK_MIN_USDT:.0f} (no per-coin data cached yet)"

    target = symbols or list(cache.keys())
    target = [sym.upper() for sym in target if sym.upper() in cache]
    if not target:
        return f"Min order: ${SAFE_FALLBACK_MIN_USDT:.0f} (no per-coin data for these symbols)"

    # Group by min_notional value
    by_min = {}
    for sym in target:
        m = cache[sym].get("min_notional", SAFE_FALLBACK_MIN_USDT)
        by_min.setdefault(m, []).append(sym)

    if len(by_min) == 1:
        # All same — clean one-liner
        only_min = list(by_min.keys())[0]
        return f"Min order: ${only_min:.0f} across all {len(target)} listed coins"

    # Mixed minimums — show majority + exceptions
    sorted_groups = sorted(by_min.items(), key=lambda kv: -len(kv[1]))
    majority_min, majority_syms = sorted_groups[0]
    parts = [f"Min order: ${majority_min:.0f}"]
    exceptions = []
    for m, syms in sorted_groups[1:]:
        for sym in syms[:max_lines]:
            exceptions.append(f"{sym} ${m:.0f}")
    if exceptions:
        parts.append("(exceptions: " + ", ".join(exceptions[:max_lines]) + ")")
    return " ".join(parts)


def get_status() -> dict:
    """Snapshot for /exchange_rules endpoint or admin views."""
    s = _load_state()
    return {
        "global_floor_usdt":  s.get("global_floor", BOT_GLOBAL_FLOOR_USDT),
        "fallback_min_usdt":  s.get("fallback_min", SAFE_FALLBACK_MIN_USDT),
        "cached_symbols":     len(s.get("binance_us", {})),
        "last_full_scan":     s.get("last_full_scan"),
        "scan_count":         s.get("scan_count", 0),
        "cache_ttl_hours":    CACHE_TTL_HOURS,
        "rules":              s.get("binance_us", {}),
    }
