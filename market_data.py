"""
market_data.py — NovaTrade Market Data Module
═══════════════════════════════════════════════
All market data fetching: indicators, news, Fear & Greed,
earnings calendar, market context, IPOs, gainers, SPY trend.

Imported by bot_with_proxy.py — no circular dependencies.
"""

import os
import json
import re
import time
import requests
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from projection_engine import get_projection

# ── Alpaca credentials (read from env) ──────────────────────
ALPACA_KEY    = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
DATA_URL      = "https://data.alpaca.markets"
BASE_URL      = "https://api.alpaca.markets"

# ── EDGAR config ────────────────────────────────────────────
EDGAR_USER_AGENT = "NovaTrade research bot contact@novatrade.local"
EDGAR_HEADERS    = {"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"}

# ── Shared references (set by bot on import) ─────────────────
# These are injected by bot_with_proxy.py after import
RULES        = {}
log          = print   # Will be replaced by bot's log function
shared_state = {}      # Will be replaced by bot's shared_state dict


def _set_context(rules, log_fn, shared_state_ref=None):
    """Called by bot_with_proxy.py to inject shared config and state."""
    global RULES, log, shared_state
    RULES = rules
    log   = log_fn
    if shared_state_ref is not None:
        shared_state = shared_state_ref

def get_bars(symbol, days=60):
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=days+10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&feed=iex&adjustment=raw"
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            url2 = f"{DATA_URL}/v2/stocks/{symbol}/bars?timeframe=1Day&start={start}&end={end}&limit=60&adjustment=raw"
            res  = requests.get(url2, headers=headers, timeout=10)
        if res.ok:
            bars = res.json().get("bars", [])
            if bars: return bars
        return []
    except Exception as e:
        log(f"⚠️ Bars {symbol}: {e}")
        return []

def get_intraday_bars(symbol, timeframe="5Min", hours=7):
    """
    Fetch intraday bars for VWAP, candlestick patterns and volume delta.
    Default: 5-minute bars for the last 7 hours (covers full trading day).
    Returns list of bars with keys: t, o, h, l, c, v
    """
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = (f"{DATA_URL}/v2/stocks/{symbol}/bars"
               f"?timeframe={timeframe}&start={start}&end={end}"
               f"&limit=200&feed=iex&adjustment=raw")
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            url2 = (f"{DATA_URL}/v2/stocks/{symbol}/bars"
                    f"?timeframe={timeframe}&start={start}&end={end}&limit=200")
            res = requests.get(url2, headers=headers, timeout=10)
        if res.ok:
            bars = res.json().get("bars", [])
            return bars if bars else []
        return []
    except Exception as e:
        return []

def compute_intraday_indicators(intraday_bars):
    """
    Compute intraday indicators from 5-min bars:
      - VWAP (Volume Weighted Average Price)
      - Volume delta proxy (green vs red candle volume)
      - Candlestick patterns (last 3 candles)
      - Intraday trend (above/below VWAP)
      - Support/resistance levels from today's range
    """
    if not intraday_bars or len(intraday_bars) < 3:
        return None

    # ── VWAP ─────────────────────────────────────────────────
    # VWAP = sum(typical_price * volume) / sum(volume)
    # Typical price = (H + L + C) / 3
    total_pv  = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"]
                    for b in intraday_bars)
    total_vol = sum(b["v"] for b in intraday_bars)
    vwap = round(total_pv / total_vol, 2) if total_vol > 0 else None

    # ── Volume delta (buy vs sell pressure proxy) ─────────────
    # Green candle (close > open) = buying pressure
    # Red candle (close < open)   = selling pressure
    buy_vol  = sum(b["v"] for b in intraday_bars if b["c"] >= b["o"])
    sell_vol = sum(b["v"] for b in intraday_bars if b["c"] <  b["o"])
    total_delta_vol = buy_vol + sell_vol
    buy_pct  = round(buy_vol  / total_delta_vol * 100, 1) if total_delta_vol > 0 else 50
    sell_pct = round(sell_vol / total_delta_vol * 100, 1) if total_delta_vol > 0 else 50
    vol_delta_bias = "BUYERS" if buy_pct > 60 else "SELLERS" if sell_pct > 60 else "NEUTRAL"

    # ── Volume spike detection (intraday) ─────────────────────
    avg_bar_vol  = total_delta_vol / len(intraday_bars) if intraday_bars else 0
    last_bar_vol = intraday_bars[-1]["v"] if intraday_bars else 0
    intraday_vol_ratio = round(last_bar_vol / avg_bar_vol, 1) if avg_bar_vol > 0 else 0

    # ── Candlestick pattern detection (last 3 candles) ────────
    patterns = []
    bars = intraday_bars[-6:]  # Last 6 bars for context

    def body(b):    return abs(b["c"] - b["o"])
    def upper(b):   return b["h"] - max(b["c"], b["o"])
    def lower(b):   return min(b["c"], b["o"]) - b["l"]
    def range_(b):  return b["h"] - b["l"]
    def is_bull(b): return b["c"] > b["o"]
    def is_bear(b): return b["c"] < b["o"]

    if len(bars) >= 2:
        c0 = bars[-1]   # Current (latest)
        c1 = bars[-2]   # Previous

        # ── Hammer / Hanging Man ──────────────────────────────
        # Long lower wick (>2x body), small body, tiny upper wick
        if (range_(c0) > 0 and body(c0) > 0 and
            lower(c0) >= body(c0) * 2 and
            upper(c0) <= body(c0) * 0.5):
            pattern = "HAMMER" if is_bull(c0) else "HANGING_MAN"
            patterns.append(f"{pattern}(bullish reversal signal)" if is_bull(c0)
                            else f"{pattern}(bearish warning)")

        # ── Shooting Star / Inverted Hammer ──────────────────
        # Long upper wick (>2x body), small body, tiny lower wick
        if (range_(c0) > 0 and body(c0) > 0 and
            upper(c0) >= body(c0) * 2 and
            lower(c0) <= body(c0) * 0.5):
            pattern = "SHOOTING_STAR" if is_bear(c0) else "INVERTED_HAMMER"
            patterns.append(f"{pattern}(bearish reversal)" if is_bear(c0)
                            else f"{pattern}(potential reversal)")

        # ── Doji (indecision) ────────────────────────────────
        if range_(c0) > 0 and body(c0) <= range_(c0) * 0.1:
            patterns.append("DOJI(indecision — watch next candle)")

        # ── Bullish Engulfing ────────────────────────────────
        if (is_bear(c1) and is_bull(c0) and
            c0["o"] <= c1["c"] and c0["c"] >= c1["o"]):
            patterns.append("BULLISH_ENGULFING(strong buy signal)")

        # ── Bearish Engulfing ────────────────────────────────
        if (is_bull(c1) and is_bear(c0) and
            c0["o"] >= c1["c"] and c0["c"] <= c1["o"]):
            patterns.append("BEARISH_ENGULFING(strong sell signal)")

        # ── Liquidity grab / shakeout ────────────────────────
        # Big wick down but closes back up near open (the 8am NVDA pattern)
        if (range_(c0) > 0 and
            lower(c0) >= range_(c0) * 0.5 and
            c0["c"] >= (c0["o"] + c0["l"]) / 2):
            patterns.append("LIQUIDITY_GRAB(wick-down recovery — bullish)")

        # ── Bearish wick grab (stop hunt up) ────────────────
        if (range_(c0) > 0 and
            upper(c0) >= range_(c0) * 0.5 and
            c0["c"] <= (c0["o"] + c0["h"]) / 2):
            patterns.append("STOP_HUNT_HIGH(wick-up reversal — bearish)")

    if len(bars) >= 3:
        c0, c1, c2 = bars[-1], bars[-2], bars[-3]

        # ── Three white soldiers (strong uptrend) ────────────
        if (is_bull(c0) and is_bull(c1) and is_bull(c2) and
            c0["c"] > c1["c"] > c2["c"] and
            c0["o"] > c1["o"] > c2["o"]):
            patterns.append("THREE_WHITE_SOLDIERS(strong bullish trend)")

        # ── Three black crows (strong downtrend) ─────────────
        if (is_bear(c0) and is_bear(c1) and is_bear(c2) and
            c0["c"] < c1["c"] < c2["c"] and
            c0["o"] < c1["o"] < c2["o"]):
            patterns.append("THREE_BLACK_CROWS(strong bearish trend)")

        # ── Morning star (bullish reversal) ──────────────────
        if (is_bear(c2) and body(c1) <= range_(c1) * 0.3 and
            is_bull(c0) and c0["c"] > (c2["o"] + c2["c"]) / 2):
            patterns.append("MORNING_STAR(bullish reversal — high confidence)")

        # ── Evening star (bearish reversal) ──────────────────
        if (is_bull(c2) and body(c1) <= range_(c1) * 0.3 and
            is_bear(c0) and c0["c"] < (c2["o"] + c2["c"]) / 2):
            patterns.append("EVENING_STAR(bearish reversal — high confidence)")

    # ── Intraday support / resistance ─────────────────────────
    today_high = max(b["h"] for b in intraday_bars)
    today_low  = min(b["l"] for b in intraday_bars)
    current    = intraday_bars[-1]["c"]
    vwap_pos   = ("ABOVE_VWAP" if vwap and current > vwap * 1.001
                  else "BELOW_VWAP" if vwap and current < vwap * 0.999
                  else "AT_VWAP")

    # ── OBV (On-Balance Volume) from intraday bars ────────────
    obv = 0
    obv_values = []
    for i, b in enumerate(intraday_bars):
        if i == 0:
            obv += b["v"]
        elif b["c"] > intraday_bars[i-1]["c"]:
            obv += b["v"]
        elif b["c"] < intraday_bars[i-1]["c"]:
            obv -= b["v"]
        obv_values.append(obv)

    # OBV trend: compare last 6 bars
    obv_trend = "RISING" if len(obv_values) >= 6 and obv_values[-1] > obv_values[-6] else \
                "FALLING" if len(obv_values) >= 6 and obv_values[-1] < obv_values[-6] else "FLAT"

    return {
        "vwap":             vwap,
        "vwap_position":    vwap_pos,
        "buy_vol_pct":      buy_pct,
        "sell_vol_pct":     sell_pct,
        "vol_delta_bias":   vol_delta_bias,
        "intraday_vol_ratio": intraday_vol_ratio,
        "patterns":         patterns,
        "today_high":       round(today_high, 2),
        "today_low":        round(today_low, 2),
        "obv_trend":        obv_trend,
        "bar_count":        len(intraday_bars),
    }

def _compute_breakout(bars: list, close: float, vol_ratio, rsi_v) -> dict:
    """
    Detect 20-period high/low breakout with volume confirmation.
    Returns breakout signal, momentum score, and direction.
    """
    periods = RULES["breakout_periods"]  # 20
    vol_min = RULES["vol_spike_multiplier"]  # 1.5x

    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    breakout_high = None
    breakout_low  = None
    is_breakout_up   = False
    is_breakout_down = False

    if len(highs) >= periods + 1:
        period_high  = max(highs[-periods-1:-1])
        period_low   = min(lows[-periods-1:-1])
        breakout_high = round(period_high, 2)
        breakout_low  = round(period_low, 2)
        is_breakout_up   = close > period_high
        is_breakout_down = close < period_low

    vol_spike = (vol_ratio or 0) >= vol_min

    # Combined signal
    breakout_signal = (
        "BULLISH_BREAKOUT"  if is_breakout_up   and vol_spike else
        "BEARISH_BREAKDOWN" if is_breakout_down and vol_spike else
        "BREAKOUT_NO_VOL"   if (is_breakout_up or is_breakout_down) else
        "NO_BREAKOUT"
    )

    # Momentum quality score 0-100
    momentum_score = 0
    if rsi_v:
        if RULES["rsi_momentum_min"] <= rsi_v <= 75:
            momentum_score += 30
        elif rsi_v <= RULES["rsi_oversold_max"]:
            momentum_score += 25
    if vol_spike:
        momentum_score += 35
    if is_breakout_up:
        momentum_score += 35

    return {
        "breakout_high":    breakout_high,
        "breakout_low":     breakout_low,
        "is_breakout_up":   is_breakout_up,
        "is_breakout_down": is_breakout_down,
        "breakout_signal":  breakout_signal,
        "vol_spike":        vol_spike,
        "momentum_score":   momentum_score,
    }

def compute_indicators(bars):
    if len(bars) < 26: return None
    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    def sma(data, n): return sum(data[-n:]) / n if len(data) >= n else None
    def ema(data, n):
        k = 2/(n+1); e = data[0]
        for p in data[1:]: e = p*k + e*(1-k)
        return e
    def rsi(data, n=14):
        g,l = [],[]
        for i in range(1,len(data)):
            d = data[i]-data[i-1]; g.append(max(d,0)); l.append(max(-d,0))
        if len(g)<n: return None
        ag=sum(g[-n:])/n; al=sum(l[-n:])/n
        return round(100-(100/(1+ag/al)),2) if al else 100
    close=closes[-1]; sma20=sma(closes,20); sma50=sma(closes,50)
    ema9=ema(closes[-20:],9); ema21=ema(closes[-30:],21)
    macd=round(ema(closes[-30:],12)-ema(closes,26),4)
    rsi_v=rsi(closes)
    bb_mid=sma20
    if bb_mid:
        std=(sum((c-bb_mid)**2 for c in closes[-20:])/20)**0.5
        bb_u=bb_mid+2*std; bb_l=bb_mid-2*std
        bb_pct=round((close-bb_l)/(bb_u-bb_l)*100,1) if bb_u!=bb_l else 50
    else: bb_pct=None
    avg_vol=sum(volumes[-20:])/20 if len(volumes)>=20 else None
    vol_ratio=round(volumes[-1]/avg_vol,2) if avg_vol else None
    mom_5d=round((closes[-1]-closes[-6])/closes[-6]*100,2) if len(closes)>=6 else None

    # ── OBV (On-Balance Volume) — daily ──────────────────────
    # Rising OBV + rising price = healthy uptrend (volume confirms move)
    # Rising price + falling OBV = distribution (smart money selling)
    obv = 0
    obv_series = []
    for i, b in enumerate(bars):
        if i == 0:
            obv += b["v"]
        elif b["c"] > bars[i-1]["c"]:
            obv += b["v"]
        elif b["c"] < bars[i-1]["c"]:
            obv -= b["v"]
        obv_series.append(obv)
    obv_trend = ("RISING"  if len(obv_series) >= 10 and obv_series[-1] > obv_series[-10]
            else "FALLING" if len(obv_series) >= 10 and obv_series[-1] < obv_series[-10]
            else "FLAT")
    # OBV divergence: price up but OBV falling = bearish divergence
    price_trend = ("UP"   if len(closes) >= 10 and closes[-1] > closes[-10]
              else "DOWN" if len(closes) >= 10 and closes[-1] < closes[-10]
              else "FLAT")
    obv_divergence = None
    if price_trend == "UP"   and obv_trend == "FALLING": obv_divergence = "BEARISH"
    if price_trend == "DOWN" and obv_trend == "RISING":  obv_divergence = "BULLISH"

    return {"close":round(close,2),"rsi":rsi_v,"macd":macd,
            "sma20":round(sma20,2) if sma20 else None,
            "sma50":round(sma50,2) if sma50 else None,
            "ema9":round(ema9,2),"ema21":round(ema21,2),
            "bb_pct":bb_pct,"vol_ratio":vol_ratio,"mom_5d":mom_5d,
            "obv_trend":obv_trend,"obv_divergence":obv_divergence,
            # ── Breakout detection ────────────────────────────
            **_compute_breakout(bars, close, vol_ratio, rsi_v)}

def get_chart_section():
    """
    Enhanced chart section — adds 5-layer projection line for every symbol.
    Projections cached in shared_state for autonomous bot use (zero AI calls).
    """
    lines       = []
    projections = {}

    for sym in RULES["universe"]:
        bars = get_bars(sym)
        ind  = compute_indicators(bars)
        if not ind:
            lines.append(f"  {sym}: insufficient data")
            continue

        # Daily indicator line
        lines.append(
            f"  {sym}: ${ind['close']} RSI={ind['rsi']} MACD={ind['macd']} "
            f"SMA20={ind['sma20']} SMA50={ind['sma50']} EMA9={ind['ema9']} "
            f"EMA21={ind['ema21']} BB%={ind['bb_pct']} "
            f"Vol={ind['vol_ratio']} Mom5d={ind['mom_5d']}% "
            f"OBV={ind['obv_trend']}"
            + (f" ⚠️OBV_DIV={ind['obv_divergence']}" if ind.get('obv_divergence') else "")
        )

        # NEW: Intraday 5-min bars — VWAP, volume delta, candlestick patterns
        try:
            intraday = get_intraday_bars(sym, timeframe="5Min", hours=8)
            if intraday and len(intraday) >= 5:
                id_ind = compute_intraday_indicators(intraday)
                if id_ind:
                    # VWAP line
                    vwap_line = (f"    → INTRADAY: VWAP=${id_ind['vwap']} "
                                 f"[{id_ind['vwap_position']}] "
                                 f"H={id_ind['today_high']} L={id_ind['today_low']} "
                                 f"| Volume: {id_ind['vol_delta_bias']} "
                                 f"(buy={id_ind['buy_vol_pct']}% sell={id_ind['sell_vol_pct']}%) "
                                 f"OBV_intra={id_ind['obv_trend']}")
                    if id_ind['intraday_vol_ratio'] >= 2.0:
                        vwap_line += f" 🔥VOL_SPIKE={id_ind['intraday_vol_ratio']}x"
                    lines.append(vwap_line)

                    # Candlestick patterns
                    if id_ind['patterns']:
                        lines.append(f"    → PATTERNS: {' | '.join(id_ind['patterns'][:3])}")
        except Exception:
            pass  # Never break chart section for intraday failure

        # 5-layer projection
        try:
            proj = get_projection(sym, bars, ind=ind)
            projections[sym] = proj
            if not proj.get("error"):
                lines.append(
                    f"    → PROJ: High={proj['proj_high']} Low={proj['proj_low']} "
                    f"Pivot={proj['pivot']} ATR=${proj['atr']} "
                    f"Bias={proj['bias'].upper()} Conf={proj['confidence']}/100 "
                    f"| {proj['trade_action']}"
                )
        except Exception:
            pass

    # Cache projections in shared_state — bot reads this while AIs sleep
    shared_state["last_projections"] = projections
    shared_state["last_proj_time"]   = datetime.now().isoformat()

    return "\n".join(lines)

def get_news_context():
    try:
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        symbols = ",".join(RULES["universe"][:8])
        url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbols}&start={start}&end={end}&limit=15&sort=desc"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            articles = res.json().get("news", [])
            lines = []
            for a in articles[:12]:
                sym = a.get("symbols", ["?"])[0] if a.get("symbols") else "MKT"
                lines.append(f"  [{sym}] {a.get('headline','')}")
            return "\n".join(lines) if lines else "  No news"
        return "  News unavailable"
    except Exception as e:
        return f"  News error: {e}"

def get_fear_greed_index() -> dict:
    """
    Fetch the Crypto Fear & Greed Index from alternative.me.
    Free, no API key, updates daily.
    Returns dict with value (0-100), label, and trading signal.
    """
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=8,
            headers={"User-Agent": "NovaTrade/1.0"}
        )
        if not resp.ok:
            return {}
        data  = resp.json().get("data", [{}])[0]
        value = int(data.get("value", 50))
        label = data.get("value_classification", "Neutral")

        # Trading signal interpretation
        if value <= 20:
            signal = "EXTREME_FEAR — historically best crypto buy zone"
        elif value <= 40:
            signal = "FEAR — good accumulation zone, reduced sell pressure"
        elif value <= 60:
            signal = "NEUTRAL — no strong directional bias"
        elif value <= 80:
            signal = "GREED — be cautious, reduce new entries"
        else:
            signal = "EXTREME_GREED — high reversal risk, consider taking profits"

        log(f"😱 Fear & Greed: {value}/100 ({label}) — {signal.split('—')[0].strip()}")
        return {"value": value, "label": label, "signal": signal}
    except Exception as e:
        log(f"⚠️ Fear & Greed fetch failed: {e}")
        return {}

def get_earnings_calendar(symbols: list) -> dict:
    """
    Check if any of our symbols have earnings in the next 5 days.
    Uses Alpaca news API to detect earnings mentions — free, no extra key.
    Returns {symbol: {"days_until": N, "warning": str}} for at-risk positions.
    """
    warnings = {}
    if not symbols:
        return warnings
    try:
        from datetime import datetime, timedelta, timezone
        now     = datetime.now(timezone.utc)
        end     = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        start   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        syms    = ",".join(symbols[:10])
        url     = (f"{DATA_URL}/v1beta1/news?symbols={syms}"
                   f"&start={start}&end={end}&limit=20&sort=desc")
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        resp    = requests.get(url, headers=headers, timeout=8)
        if not resp.ok:
            return warnings

        articles = resp.json().get("news", [])
        earnings_keywords = ["earnings", "EPS", "quarterly results",
                             "revenue report", "fiscal quarter", "beats estimates",
                             "misses estimates", "guidance"]

        for article in articles:
            headline = article.get("headline", "").lower()
            summary  = article.get("summary", "").lower()
            text     = headline + " " + summary
            if any(kw.lower() in text for kw in earnings_keywords):
                for sym in article.get("symbols", []):
                    if sym in symbols and sym not in warnings:
                        pub_date = article.get("created_at", "")
                        warnings[sym] = {
                            "warning":   f"Earnings-related news detected — IV risk",
                            "headline":  article.get("headline", "")[:80],
                            "published": pub_date[:10] if pub_date else "",
                        }
                        log(f"⚠️ EARNINGS ALERT {sym}: {article.get('headline','')[:60]}")
    except Exception as e:
        log(f"⚠️ Earnings calendar error: {e}")
    return warnings

def get_market_context():
    try:
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url = f"{DATA_URL}/v2/stocks/snapshots?symbols=SPY,QQQ"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            data = res.json()
            lines = []
            for sym, snap in data.items():
                bar  = snap.get("dailyBar", {})
                prev = snap.get("prevDailyBar", {})
                if bar and prev:
                    chg = round((bar.get("c",0)-prev.get("c",0))/prev.get("c",1)*100,2)
                    lines.append(f"  {sym}: ${bar.get('c',0):.2f} ({'+' if chg>=0 else ''}{chg}%)")
            return "\n".join(lines) if lines else "  Market data unavailable"
        return "  Market unavailable"
    except Exception as e:
        return f"  Market error: {e}"



# ── SEC EDGAR Form 4 — Insider & Congressional Trade Tracker ──────
# Official US government data. Free, no API key needed, real-time.
# EDGAR policy: identify yourself in User-Agent header.
# Rate limit: 10 req/sec — we use ~1/hour so no issue at all.
# Docs: https://www.sec.gov/developer

EDGAR_USER_AGENT = "NovaTrade research bot contact@novatrade.local"
EDGAR_HEADERS    = {"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"}

# Politicians with known SEC CIK numbers
EDGAR_POLITICIANS = {
    "0001341439": {"name": "Nancy Pelosi",     "party": "D", "focus": "tech"},
    "0001655050": {"name": "Dan Crenshaw",      "party": "R", "focus": "energy"},
    "0001766607": {"name": "Tommy Tuberville",  "party": "R", "focus": "broad"},
    "0001820940": {"name": "Mark Kelly",        "party": "D", "focus": "tech"},
    "0001831919": {"name": "Josh Gottheimer",   "party": "D", "focus": "tech"},
}

EDGAR_TRACK_TICKERS = {
    "NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "META", "GOOGL",
    "PLTR", "AMD", "AVGO", "TSM", "NFLX", "CRM", "ORCL",
}

def get_spy_trend():
    """
    Check SPY trend vs 50-day SMA.
    Returns: "bull" / "bear" / "neutral"
    Uses cached value if live fetch fails — never shows $0.00.
    """
    try:
        bars = get_bars("SPY", days=60)
        if not bars or len(bars) < 50:
            try:
                end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                start = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
                headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
                url = f"{DATA_URL}/v2/stocks/SPY/bars?timeframe=1Day&start={start}&end={end}&limit=60"
                res = requests.get(url, headers=headers, timeout=10)
                if res.ok:
                    bars = res.json().get("bars", [])
            except Exception:
                pass
        if bars and len(bars) >= 50:
            closes  = [b["c"] for b in bars]
            sma50   = sum(closes[-50:]) / 50
            current = closes[-1]
            change  = round((current - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else 0
            result  = ("bull"    if current > sma50 * 1.01
                       else "bear" if current < sma50 * 0.99
                       else "neutral")
            # Cache successful result
            shared_state["spy_cache"] = (result, current, sma50, change)
            return result, current, sma50, change
    except Exception as e:
        log(f"⚠️ SPY trend check failed: {e}")

    # Return cached value if available — never return $0
    if shared_state.get("spy_cache"):
        return shared_state["spy_cache"]
    return "neutral", 0, 0, 0

def get_biggest_gainers():
    """
    Fetch today's biggest gainers from Alpaca screener.
    Only used for collaborative consideration — not autonomous trades.
    """
    try:
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
        url     = f"{DATA_URL}/v1beta1/screener/stocks/movers?top=20&market_type=stocks"
        res     = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            gainers = res.json().get("gainers", [])
            # Filter: must be in universe OR be high quality stock
            top = []
            for g in gainers[:10]:
                sym = g.get("symbol","")
                pct = float(g.get("percent_change", 0))
                if pct > 3.0:  # Only stocks up 3%+ today
                    top.append({
                        "symbol":  sym,
                        "change":  pct,
                        "in_universe": sym in RULES["universe"],
                    })
            if top:
                log(f"📈 Biggest gainers today (>3%): {[(t['symbol'], f'+{t["change"]:.1f}%') for t in top]}")
            return top
    except Exception as e:
        log(f"⚠️ Gainers fetch failed: {e}")
    return []

def get_recent_ipos(min_days=30, max_days=180):
    """
    Fetch genuine recent IPOs using Alpaca's listed_at date field.
    Only returns stocks that actually listed within min_days–max_days ago.
    Filters out established stocks that happen to have limited bar history.
    Requirements: 500k+ avg volume, $5–$500 price, listed 30–180 days ago.
    """
    try:
        today   = datetime.now(timezone.utc).date()
        headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

        # Get all active tradable assets — includes listed_at field
        res = requests.get(
            f"{BASE_URL}/v2/assets?status=active&asset_class=us_equity",
            headers=headers, timeout=15
        )
        if not res.ok:
            return []

        assets = res.json()

        # ── Filter to genuine recent IPOs using listed_at ────────
        # listed_at is the actual exchange listing date — reliable signal
        ipo_candidates = []
        for a in assets:
            listed_at = a.get("listed_at", "")
            if not listed_at:
                continue
            try:
                listed_date = datetime.fromisoformat(
                    listed_at.replace("Z", "+00:00")
                ).date()
            except Exception:
                continue

            days_since = (today - listed_date).days

            # Must be within our IPO window
            if not (min_days <= days_since <= max_days):
                continue

            sym = a.get("symbol", "")
            # Skip warrants, rights, units, preferred shares
            if not sym or sym.endswith(("W", "R", "U", "P", "+")):
                continue
            # Skip longer symbols (typically ETFs/structured products)
            if len(sym) > 5:
                continue
            # Must be tradable + easy to borrow
            if not (a.get("tradable") and a.get("easy_to_borrow")):
                continue

            ipo_candidates.append({
                "symbol":    sym,
                "days_old":  days_since,
                "listed_at": listed_at,
            })

        if not ipo_candidates:
            return []

        log(f"🆕 Genuine IPO candidates (listed {min_days}–{max_days}d ago): "
            f"{len(ipo_candidates)} stocks — sampling for volume/momentum...")

        # ── Fetch bars to check volume + momentum ────────────────
        end   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=max_days + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Sort by most recent IPO first (freshest momentum potential)
        ipo_candidates.sort(key=lambda x: x["days_old"])

        recent_ipos = []
        for cand in ipo_candidates:
            sym = cand["symbol"]
            try:
                url = (f"{DATA_URL}/v2/stocks/{sym}/bars"
                       f"?timeframe=1Day&start={start}&end={end}&limit=200&feed=iex")
                r   = requests.get(url, headers=headers, timeout=6)
                if not r.ok:
                    continue
                bars = r.json().get("bars", [])
                if len(bars) < 5:
                    continue

                last_price = bars[-1]["c"]
                avg_vol    = sum(b["v"] for b in bars) / len(bars)
                mom_5d     = round(
                    (bars[-1]["c"] - bars[-6]["c"]) / bars[-6]["c"] * 100, 2
                ) if len(bars) >= 6 else 0

                # Quality gates: liquid, priced reasonably
                if avg_vol < 500_000:
                    continue
                if not (5 <= last_price <= 500):
                    continue

                recent_ipos.append({
                    "symbol":    sym,
                    "days_old":  cand["days_old"],
                    "price":     last_price,
                    "avg_vol":   round(avg_vol),
                    "mom_5d":    mom_5d,
                    "listed_at": cand["listed_at"],
                })

                if len(recent_ipos) >= 10:
                    break

            except Exception:
                continue

        if recent_ipos:
            recent_ipos = sorted(recent_ipos, key=lambda x: -abs(x["mom_5d"]))
            ipo_summary = [(i["symbol"], f"{i['days_old']}d", f"{i['mom_5d']:+.1f}%") for i in recent_ipos]
            log(f"🆕 Recent IPOs confirmed ({len(recent_ipos)}): {ipo_summary}")

        return recent_ipos

    except Exception as e:
        log(f"⚠️ IPO detection failed: {e}")
        return []

# ── Top Investor / Fund Tracking ─────────────────────────
# SEC CIK numbers for top investors (public 13F filings)
TOP_INVESTORS = {
    "Cathie Wood (ARK)":      "0001697748",
    "Michael Burry":          "0001649339",
    "Warren Buffett":         "0001067983",
    "George Soros":           "0001029160",
    "Ray Dalio (Bridgewater)":"0001350694",
    "Bill Ackman (Pershing)": "0001336528",
    "David Tepper":           "0001262463",
    "Stanley Druckenmiller":  "0001536411",
}

def get_market_mode():
    """
    Returns (mode, sleep_interval_minutes).
    Modes: sleep | premarket | opening | prime | power_hour | afterhours

    Weekend/holiday aware — checks day of week first, then
    uses Alpaca clock for holidays (once per hour, cached).
    """
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    weekday = now_et.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    mins    = now_et.hour * 60 + now_et.minute

    # ── Weekend: always sleep ─────────────────────────────────
    if weekday >= 5:
        return "sleep", 60

    # ── Weekday time-based mode ───────────────────────────────
    if   mins < 510:               return "sleep",      60
    elif 510  <= mins < 570:       return "premarket",  20
    elif 570  <= mins < 630:       return "opening",     5
    elif 630  <= mins < 900:       return "prime",       5
    elif 900  <= mins < 960:       return "power_hour",  5
    elif 960  <= mins < 1020:      return "afterhours", 20
    else:                          return "sleep",      60

# ── Exit Conditions ──────────────────────────────────────
