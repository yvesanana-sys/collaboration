"""
projection_engine.py
────────────────────────────────────────────────────────────────
Elite 5-Layer Daily Price Projection Engine
Integrates directly into bot_with_proxy.py

HOW TO INTEGRATE:
  1. Drop this file in the same directory as bot_with_proxy.py
  2. Add one import line at the top of bot_with_proxy.py:
       from projection_engine import get_projection, format_projection_for_ai
  3. Call get_projection(symbol, bars) wherever you have bar data
  4. Inject format_projection_for_ai() into Claude/Grok prompts

ARCHITECTURE:
  Layer 1 (25%) — Price Structure   : Pivot points, gap analysis, open vs close
  Layer 2 (25%) — Trend Context     : EMA/SMA alignment, trendline slope
  Layer 3 (20%) — Volume Profile    : Vol ratio, OBV direction, ATR scaling
  Layer 4 (15%) — Momentum          : RSI, MACD, Stoch signals
  Layer 5 (15%) — Sentiment         : Fear & Greed proxy via VIX-like signals

OUTPUT per symbol:
  proj_high     — weighted projected daily high
  proj_low      — weighted projected daily low
  pivot         — daily pivot point
  confidence    — 0-100 score (how much to trust this projection)
  bias          — 'bullish' | 'bearish' | 'neutral'
  key_levels    — dict of R1,R2,S1,S2,pivot,support,resistance
  atr           — expected daily range in dollars
  signal_summary — plain English summary for AI prompt injection
"""

from datetime import datetime
import math


# ─────────────────────────────────────────────────────────────
# CORE COMPUTATION
# ─────────────────────────────────────────────────────────────

def compute_atr(bars, period=14):
    """Average True Range — measures daily volatility in $ terms."""
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        high  = bars[i]["h"]
        low   = bars[i]["l"]
        prev_c = bars[i-1]["c"]
        tr = max(high - low, abs(high - prev_c), abs(low - prev_c))
        true_ranges.append(tr)
    return round(sum(true_ranges[-period:]) / period, 4)


def compute_pivot_levels(prev_high, prev_low, prev_close):
    """
    Classic floor-trader pivot points.
    Most reliable when used as anchor, not final target.
    """
    p  = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * p) - prev_low
    s1 = (2 * p) - prev_high
    r2 = p + (prev_high - prev_low)
    s2 = p - (prev_high - prev_low)
    r3 = prev_high + 2 * (p - prev_low)
    s3 = prev_low  - 2 * (prev_high - p)
    return {
        "pivot": round(p,  4),
        "r1":    round(r1, 4),
        "r2":    round(r2, 4),
        "r3":    round(r3, 4),
        "s1":    round(s1, 4),
        "s2":    round(s2, 4),
        "s3":    round(s3, 4),
    }


def compute_trend_score(closes, ind):
    """
    Returns trend_score in [0, 1].
    0 = strongly bearish (price below all MAs)
    1 = strongly bullish (price above all MAs)
    0.5 = neutral / mixed
    """
    close   = closes[-1]
    signals = []

    sma20 = ind.get("sma20")
    sma50 = ind.get("sma50")
    ema9  = ind.get("ema9")
    ema21 = ind.get("ema21")

    if sma20: signals.append(1 if close > sma20 else 0)
    if sma50: signals.append(1 if close > sma50 else 0)
    if ema9:  signals.append(1 if close > ema9  else 0)
    if ema21: signals.append(1 if close > ema21 else 0)

    # Death cross / golden cross
    if sma50 and sma20:
        signals.append(1 if sma20 > sma50 else 0)  # Golden cross = bullish

    # 5-day momentum
    mom = ind.get("mom_5d")
    if mom is not None:
        signals.append(1 if mom > 0 else 0)

    if not signals:
        return 0.5
    return round(sum(signals) / len(signals), 4)


def compute_volume_multiplier(bars, ind):
    """
    Volume multiplier expands/contracts the projected range.
    >1.2 vol ratio = range expansion (breakout likely)
    <0.8 vol ratio = range contraction (choppy/fakeout risk)
    """
    vol_ratio = ind.get("vol_ratio")
    if vol_ratio is None:
        return 1.0
    # Cap expansion to 1.5x, contraction to 0.7x
    return round(max(0.70, min(1.50, vol_ratio)), 4)


def compute_momentum_adjustment(ind, atr):
    """
    RSI and MACD adjust the projected range direction.
    RSI near 30 = oversold = add upside potential
    RSI near 70 = overbought = cap upside, expand downside
    MACD sign = directional tilt
    """
    rsi  = ind.get("rsi")
    macd = ind.get("macd")
    adj  = 0.0

    if rsi is not None:
        # RSI factor: -0.2 (RSI=70, bearish) to +0.2 (RSI=30, bullish)
        rsi_factor = (50 - rsi) / 100  # range [-0.2, +0.2] for normal RSI
        adj += rsi_factor * atr * 0.20

    if macd is not None:
        macd_sign = 1 if macd > 0 else -1
        adj += macd_sign * atr * 0.05

    return round(adj, 4)


def compute_sentiment_adjustment(ind, atr, fear_greed_score=None):
    """
    Macro sentiment override layer.
    Uses vol_ratio as VIX proxy when no external F&G data available.
    If fear_greed_score provided (0-100): use directly.

    Extreme Fear (<20): expand downside by 0.15x ATR
    Extreme Greed (>80): expand upside by 0.15x ATR
    """
    # If external F&G provided, use it
    if fear_greed_score is not None:
        fg = fear_greed_score / 100.0  # normalize to [0,1]
        if fg < 0.20:    # Extreme Fear
            return round(-atr * 0.15, 4)  # negative = push low down, compress high
        elif fg > 0.80:  # Extreme Greed
            return round(+atr * 0.15, 4)  # positive = push high up
        else:
            return round((fg - 0.5) * atr * 0.10, 4)

    # Fallback: use BB% as sentiment proxy
    bb_pct = ind.get("bb_pct")
    if bb_pct is not None:
        # BB% 0 = bottom of band (fear), 100 = top (greed)
        fg_proxy = bb_pct / 100.0
        if fg_proxy < 0.20:
            return round(-atr * 0.10, 4)
        elif fg_proxy > 0.80:
            return round(+atr * 0.10, 4)
        return round((fg_proxy - 0.5) * atr * 0.08, 4)

    return 0.0


def compute_confidence(ind, bars, trend_score, vol_mult):
    """
    Confidence score 0-100.
    Higher = more reliable projection, larger position OK.
    Lower = use minimum size, widen stops.

    Components:
    - Trend agreement (all MAs aligned)   : 25 pts
    - Volume confirmation                  : 25 pts
    - Momentum clarity (RSI not at 50)    : 25 pts
    - Data quality (enough bars)           : 25 pts
    """
    score = 0

    # Trend agreement
    trend_clarity = abs(trend_score - 0.5) * 2  # 0=mixed, 1=fully aligned
    score += round(trend_clarity * 25)

    # Volume confirmation (above avg = good signal)
    vol_ratio = ind.get("vol_ratio")
    if vol_ratio is not None:
        vol_conf = min(1.0, vol_ratio / 1.5)  # 1.5x vol = max confidence
        score += round(vol_conf * 25)
    else:
        score += 10  # partial credit

    # Momentum clarity (RSI far from 50 = clearer signal)
    rsi = ind.get("rsi")
    if rsi is not None:
        rsi_clarity = abs(rsi - 50) / 50  # 0=neutral, 1=extreme
        score += round(rsi_clarity * 25)
    else:
        score += 10

    # Data quality
    bar_count = len(bars)
    data_score = min(25, int(bar_count / 2))
    score += data_score

    return min(100, score)


# ─────────────────────────────────────────────────────────────
# MAIN PROJECTION FUNCTION
# ─────────────────────────────────────────────────────────────

def get_projection(symbol, bars, ind=None, open_price=None,
                   fear_greed_score=None):
    """
    Compute the 5-layer weighted daily range projection.

    Args:
        symbol           : ticker string (for logging)
        bars             : list of OHLCV dicts from Alpaca
                           each bar: {"t","o","h","l","c","v"}
        ind              : pre-computed indicators dict (optional)
                           if None, computes from bars
        open_price       : today's actual open if market is open (optional)
                           if None, uses last close as proxy
        fear_greed_score : external F&G index 0-100 (optional)
                           if None, uses BB% as proxy

    Returns dict:
        {
            "symbol":         str,
            "proj_high":      float,
            "proj_low":       float,
            "pivot":          float,
            "atr":            float,
            "confidence":     int (0-100),
            "bias":           str ('bullish'|'bearish'|'neutral'),
            "trend_score":    float (0-1),
            "key_levels":     dict,
            "layer_details":  dict  (debug/logging),
            "signal_summary": str  (for AI prompt injection),
            "trade_action":   str  (what to do with this)
        }
    """
    if len(bars) < 20:
        return _empty_projection(symbol, "insufficient data")

    # ── Pre-compute indicators if not provided ──────────────
    if ind is None:
        ind = _compute_indicators_local(bars)
    if ind is None:
        return _empty_projection(symbol, "indicator computation failed")

    closes  = [b["c"] for b in bars]
    prev    = bars[-1]   # Previous completed day
    prev2   = bars[-2] if len(bars) >= 2 else prev

    prev_high  = float(prev["h"])
    prev_low   = float(prev["l"])
    prev_close = float(prev["c"])

    # Today's anchor: use actual open if provided, else prev close
    anchor = float(open_price) if open_price else prev_close

    # ── Layer 1: Pivot points (25% weight) ─────────────────
    pvt = compute_pivot_levels(prev_high, prev_low, prev_close)
    pivot = pvt["pivot"]

    # ── Layer 2: ATR (volatility engine) ───────────────────
    atr = compute_atr(bars)
    if atr is None or atr <= 0:
        # Fallback: 1.5% of price as ATR estimate
        atr = round(prev_close * 0.015, 4)

    # ── Layer 3: Volume multiplier ──────────────────────────
    vol_mult = compute_volume_multiplier(bars, ind)

    # ── Layer 4: Trend score and bias ──────────────────────
    trend_score  = compute_trend_score(closes, ind)
    trend_label  = ("bullish" if trend_score >= 0.6
                    else "bearish" if trend_score <= 0.4
                    else "neutral")

    # Trend asymmetry:
    # In downtrend: compress high projection, leave low open
    # In uptrend:   compress low projection, leave high open
    if trend_label == "bearish":
        high_trend_adj = -atr * (1 - trend_score) * 0.30  # compress high
        low_trend_adj  = 0.0
    elif trend_label == "bullish":
        high_trend_adj = 0.0
        low_trend_adj  = +atr * trend_score * 0.30          # compress low (tighten)
    else:
        high_trend_adj = 0.0
        low_trend_adj  = 0.0

    # ── Layer 5: Momentum adjustment ───────────────────────
    mom_adj = compute_momentum_adjustment(ind, atr)

    # ── Layer 6: Sentiment override ────────────────────────
    sent_adj = compute_sentiment_adjustment(ind, atr, fear_greed_score)

    # ── WEIGHTED COMPOSITE ─────────────────────────────────
    #
    # Base from pivots + ATR:
    #   pivot_high  = R1 (25% weight)
    #   atr_high    = anchor + (atr * vol_mult * 0.5) (20% weight)
    #   open_anchor = open_price (35% weight)
    #   prev_close  = prev_close (20% weight)
    #
    # Then apply directional adjustments:
    #   trend_adj, momentum_adj, sentiment_adj

    pivot_high = pvt["r1"]
    pivot_low  = pvt["s1"]
    atr_high   = anchor + (atr * vol_mult * 0.50)
    atr_low    = anchor - (atr * vol_mult * 0.50)

    # Weighted base
    base_high = (pivot_high * 0.25) + (atr_high * 0.20) + (anchor * 0.35) + (prev_close * 0.20)
    base_low  = (pivot_low  * 0.25) + (atr_low  * 0.20) + (anchor * 0.35) + (prev_close * 0.20)

    # Apply adjustments
    proj_high = base_high + high_trend_adj + mom_adj + sent_adj
    proj_low  = base_low  + low_trend_adj  + mom_adj + sent_adj

    # Sanity check: high must be > low, both must be > 0
    proj_high = max(proj_high, anchor * 1.001)
    proj_low  = min(proj_low,  anchor * 0.999)
    if proj_high <= proj_low:
        spread   = atr * 0.5
        mid      = (proj_high + proj_low) / 2
        proj_high = mid + spread
        proj_low  = mid - spread

    proj_high = round(proj_high, 2)
    proj_low  = round(proj_low,  2)

    # ── Confidence score ───────────────────────────────────
    confidence = compute_confidence(ind, bars, trend_score, vol_mult)

    # ── Key levels ─────────────────────────────────────────
    key_levels = {
        "r2":         round(pvt["r2"], 2),
        "r1":         round(pvt["r1"], 2),
        "pivot":      round(pivot,     2),
        "s1":         round(pvt["s1"], 2),
        "s2":         round(pvt["s2"], 2),
        "proj_high":  proj_high,
        "proj_low":   proj_low,
        "atr":        round(atr,       2),
    }

    # ── Signal summary for AI prompt injection ─────────────
    gap = round(anchor - prev_close, 2)
    gap_str = (f"gap-up ${gap:+.2f}" if gap > 0.10
               else f"gap-down ${gap:+.2f}" if gap < -0.10
               else "flat open")

    signal_summary = (
        f"{symbol} projection: High={proj_high} Low={proj_low} | "
        f"Pivot={round(pivot,2)} ATR=${round(atr,2)} | "
        f"Bias={trend_label.upper()} (trend={round(trend_score,2)}) | "
        f"Vol={round(vol_mult,2)}x avg | RSI={ind.get('rsi','?')} | "
        f"Open {gap_str} | Confidence={confidence}/100"
    )

    # ── Trade action guidance ──────────────────────────────
    rsi = ind.get("rsi") or 50
    if confidence >= 70 and trend_label == "bullish" and rsi < 60:
        trade_action = f"BUY_ZONE: entry near ${proj_low} | target ${proj_high} | stop below ${round(proj_low * 0.96, 2)}"
    elif confidence >= 70 and trend_label == "bearish" and rsi > 55:
        trade_action = f"FADE_HIGH: watch ${proj_high} for rejection | support ${proj_low}"
    elif confidence >= 50:
        trade_action = f"RANGE: watch ${proj_low}–${proj_high} | pivot ${round(pivot,2)} is key"
    else:
        trade_action = f"LOW_CONF: reduce size | wide range ${proj_low}–${proj_high}"

    return {
        "symbol":        symbol,
        "proj_high":     proj_high,
        "proj_low":      proj_low,
        "pivot":         round(pivot, 2),
        "atr":           round(atr,   2),
        "confidence":    confidence,
        "bias":          trend_label,
        "trend_score":   round(trend_score, 4),
        "vol_mult":      round(vol_mult,    4),
        "gap":           round(gap,         2),
        "gap_type":      gap_str,
        "key_levels":    key_levels,
        "layer_details": {
            "l1_pivot_high": round(pvt["r1"], 2),
            "l1_pivot_low":  round(pvt["s1"], 2),
            "l2_trend_score": round(trend_score, 4),
            "l2_high_adj":   round(high_trend_adj, 4),
            "l2_low_adj":    round(low_trend_adj,  4),
            "l3_vol_mult":   round(vol_mult, 4),
            "l4_mom_adj":    round(mom_adj,  4),
            "l5_sent_adj":   round(sent_adj, 4),
            "base_high":     round(base_high, 4),
            "base_low":      round(base_low,  4),
        },
        "signal_summary": signal_summary,
        "trade_action":   trade_action,
        "computed_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ─────────────────────────────────────────────────────────────
# MULTI-SYMBOL BATCH PROJECTION
# ─────────────────────────────────────────────────────────────

def get_projections_batch(symbols_bars_dict, fear_greed_score=None):
    """
    Compute projections for multiple symbols at once.

    Args:
        symbols_bars_dict : { "NVDA": [bars...], "AMZN": [bars...], ... }
        fear_greed_score  : optional F&G score applied to all symbols

    Returns:
        { "NVDA": projection_dict, "AMZN": projection_dict, ... }
    """
    results = {}
    for symbol, bars in symbols_bars_dict.items():
        try:
            proj = get_projection(symbol, bars,
                                  fear_greed_score=fear_greed_score)
            results[symbol] = proj
        except Exception as e:
            results[symbol] = _empty_projection(symbol, str(e))
    return results


# ─────────────────────────────────────────────────────────────
# AI PROMPT FORMATTER
# ─────────────────────────────────────────────────────────────

def format_projection_for_ai(projections, include_low_conf=False):
    """
    Format projection data as a compact string for injection
    into Claude and Grok prompts.

    Args:
        projections      : dict from get_projections_batch()
        include_low_conf : include symbols with confidence < 50

    Returns:
        multi-line string ready to embed in prompt
    """
    lines = ["DAILY RANGE PROJECTIONS (5-layer model):"]

    # Sort by confidence descending
    sorted_syms = sorted(
        projections.items(),
        key=lambda x: x[1].get("confidence", 0),
        reverse=True
    )

    for sym, p in sorted_syms:
        if p.get("error"):
            continue
        conf = p.get("confidence", 0)
        if not include_low_conf and conf < 40:
            continue

        bias    = p.get("bias", "?").upper()
        high    = p.get("proj_high", "?")
        low     = p.get("proj_low",  "?")
        pivot   = p.get("pivot",     "?")
        atr     = p.get("atr",       "?")
        action  = p.get("trade_action", "")

        # Confidence emoji
        conf_tag = ("🟢" if conf >= 70
                    else "🟡" if conf >= 50
                    else "🔴")

        lines.append(
            f"  {conf_tag} {sym} [{bias}] conf={conf} | "
            f"proj={low}–{high} | pivot={pivot} | ATR=${atr} | "
            f"{action}"
        )

    lines.append(
        "NOTE: proj_high/low = statistical range, not guaranteed. "
        "confidence>=70=full size, 50-69=half size, <50=watch only."
    )
    return "\n".join(lines)


def format_single_projection_for_ai(proj):
    """
    Format a single symbol projection for inline AI context.
    Use this in per-position prompts or when analyzing one ticker.
    """
    if proj.get("error"):
        return f"  Projection unavailable: {proj.get('error')}"

    sym    = proj["symbol"]
    high   = proj["proj_high"]
    low    = proj["proj_low"]
    pivot  = proj["pivot"]
    atr    = proj["atr"]
    conf   = proj["confidence"]
    bias   = proj["bias"].upper()
    levels = proj["key_levels"]
    action = proj["trade_action"]
    gap    = proj.get("gap_type", "")

    return (
        f"  {sym} range projection:\n"
        f"    Projected High: ${high}  |  Projected Low: ${low}\n"
        f"    Pivot: ${pivot}  |  ATR: ${atr}  |  Open: {gap}\n"
        f"    Key levels: R2=${levels['r2']}  R1=${levels['r1']}  "
        f"S1=${levels['s1']}  S2=${levels['s2']}\n"
        f"    Bias: {bias}  |  Confidence: {conf}/100\n"
        f"    Action: {action}"
    )


# ─────────────────────────────────────────────────────────────
# BOT INTEGRATION HOOKS
# ─────────────────────────────────────────────────────────────

def build_projection_context(universe, get_bars_fn, get_indicators_fn=None,
                              fear_greed_score=None):
    """
    Convenience wrapper that takes the bot's universe list and
    get_bars function, runs projections for all symbols, and
    returns both raw data and a formatted string for prompts.

    Usage in bot_with_proxy.py:
        from projection_engine import build_projection_context
        proj_context, projections = build_projection_context(
            RULES["universe"],
            get_bars,                    # bot's existing get_bars()
            compute_indicators,          # bot's existing compute_indicators()
        )
        # Then inject proj_context into Claude/Grok prompts

    Args:
        universe         : list of ticker symbols
        get_bars_fn      : function(symbol) -> list of OHLCV bars
        get_indicators_fn: function(bars) -> indicator dict (optional)
        fear_greed_score : external F&G 0-100 (optional)

    Returns:
        (proj_string, projections_dict)
    """
    projections = {}
    for sym in universe:
        try:
            bars = get_bars_fn(sym)
            if not bars or len(bars) < 20:
                continue
            ind = get_indicators_fn(bars) if get_indicators_fn else None
            proj = get_projection(sym, bars, ind=ind,
                                  fear_greed_score=fear_greed_score)
            projections[sym] = proj
        except Exception as e:
            projections[sym] = _empty_projection(sym, str(e))

    proj_string = format_projection_for_ai(projections)
    return proj_string, projections


def get_position_exit_guidance(symbol, bars, ind, entry_price,
                                current_price, pnl_pct):
    """
    Use the projection to generate smarter exit guidance.
    Instead of fixed 7% TP, use projected high as dynamic target.

    Returns:
        {
            "hold":       bool   — should we hold or exit
            "reason":     str    — human-readable reason
            "exit_price": float  — where to set limit sell
            "stop_price": float  — where to set stop loss
        }
    """
    proj = get_projection(symbol, bars, ind=ind,
                          open_price=current_price)

    if proj.get("error"):
        return {
            "hold":       pnl_pct < 0.07,
            "reason":     "projection unavailable, using fixed TP",
            "exit_price": round(entry_price * 1.07, 2),
            "stop_price": round(entry_price * 0.96, 2),
        }

    proj_high = proj["proj_high"]
    proj_low  = proj["proj_low"]
    pivot     = proj["pivot"]
    atr       = proj["atr"]
    conf      = proj["confidence"]
    bias      = proj["bias"]

    # Dynamic exit at projected high if bullish, or pivot if bearish
    if bias == "bullish" and conf >= 60:
        exit_price = proj_high
        reason = (f"proj_high=${proj_high} (bullish bias, conf={conf}) | "
                  f"ATR=${atr}")
    elif bias == "bearish":
        # In downtrend: take profit sooner at pivot
        exit_price = min(proj_high, pivot + atr * 0.3)
        reason = (f"bearish trend — targeting ${exit_price:.2f} "
                  f"(pivot=${pivot})")
    else:
        exit_price = proj_high
        reason = f"neutral — proj_high=${proj_high}"

    # ── HARD FLOOR: exit price must ALWAYS be above entry ────
    # Minimum 1% profit above entry — never sell at a loss as "take profit"
    min_exit = round(entry_price * 1.01, 2)
    if exit_price <= entry_price:
        exit_price = min_exit
        reason = (f"exit floor applied (proj was below entry) — "
                  f"min exit ${min_exit} (+1%)")
    elif exit_price < min_exit:
        exit_price = min_exit
        reason = f"{reason} [floored to +1% min = ${min_exit}]"

    # Stop: below proj_low or hard -4%, whichever is closer to entry
    stop_proj  = proj_low * 0.995
    stop_hard  = entry_price * 0.96
    stop_price = max(stop_proj, stop_hard)

    # Stop also must be BELOW entry (never stop above what we paid)
    if stop_price >= entry_price:
        stop_price = round(entry_price * 0.96, 2)

    should_hold = current_price < exit_price * 0.99

    return {
        "hold":         should_hold,
        "reason":       reason,
        "exit_price":   round(exit_price, 2),
        "stop_price":   round(stop_price, 2),
        "proj_high":    proj_high,
        "proj_low":     proj_low,
        "confidence":   conf,
        "bias":         bias,
    }


def score_buy_opportunity(symbol, bars, ind, cash_available):
    """
    Score a potential buy using the projection.
    Returns a score 0-100 and a recommendation.

    This REPLACES/AUGMENTS the simple autopilot scoring in the bot.
    Higher score = better entry opportunity.
    """
    proj = get_projection(symbol, bars, ind=ind)

    if proj.get("error"):
        return 0, "projection failed"

    score = 0
    reasons = []

    close     = bars[-1]["c"] if bars else 0
    proj_low  = proj["proj_low"]
    proj_high = proj["proj_high"]
    pivot     = proj["pivot"]
    atr       = proj["atr"]
    conf      = proj["confidence"]
    bias      = proj["bias"]
    rsi       = ind.get("rsi") if ind else None
    macd      = ind.get("macd") if ind else None
    vol_ratio = ind.get("vol_ratio") if ind else None

    # Price near projected low = good entry (value zone)
    if close <= proj_low * 1.005:
        score += 25
        reasons.append(f"price at proj low ${proj_low}")
    elif close <= pivot:
        score += 10
        reasons.append("price below pivot")

    # Bullish bias
    if bias == "bullish":
        score += 20
        reasons.append("bullish trend")
    elif bias == "neutral":
        score += 8

    # RSI conditions
    if rsi is not None:
        if rsi < 35:
            score += 20
            reasons.append(f"RSI oversold {rsi}")
        elif rsi < 45:
            score += 12
            reasons.append(f"RSI recovering {rsi}")
        elif rsi > 65:
            score -= 10  # overbought penalty

    # MACD signal
    if macd is not None and macd > 0:
        score += 10
        reasons.append(f"MACD positive {macd}")

    # Volume confirmation
    if vol_ratio is not None and vol_ratio >= 1.2:
        score += 10
        reasons.append(f"vol spike {vol_ratio}x")

    # Projection confidence bonus
    if conf >= 70:
        score += 15
        reasons.append(f"high conf {conf}")
    elif conf >= 50:
        score += 7

    # Reward/risk ratio check
    if close > 0 and proj_high > close and proj_low < close:
        upside   = (proj_high - close) / close
        downside = (close - proj_low) / close
        if downside > 0 and upside / downside >= 1.5:
            score += 10
            reasons.append(f"R:R={upside/downside:.1f}x")

    score = max(0, min(100, score))

    # Position sizing hint
    if score >= 75:
        sizing = "full"
    elif score >= 55:
        sizing = "half"
    else:
        sizing = "skip"

    summary = (f"score={score} sizing={sizing} | "
               + " | ".join(reasons[:4]))

    return score, summary


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _compute_indicators_local(bars):
    """
    Standalone indicator computation (mirrors bot's compute_indicators).
    Used when indicators are not pre-provided.
    """
    if len(bars) < 26:
        return None
    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]

    def sma(data, n):
        return sum(data[-n:]) / n if len(data) >= n else None

    def ema(data, n):
        k = 2 / (n + 1)
        e = data[0]
        for p in data[1:]:
            e = p * k + e * (1 - k)
        return e

    def rsi_calc(data, n=14):
        g, l = [], []
        for i in range(1, len(data)):
            d = data[i] - data[i-1]
            g.append(max(d, 0))
            l.append(max(-d, 0))
        if len(g) < n:
            return None
        ag = sum(g[-n:]) / n
        al = sum(l[-n:]) / n
        return round(100 - (100 / (1 + ag / al)), 2) if al else 100

    close = closes[-1]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    ema9  = ema(closes[-20:], 9)
    ema21 = ema(closes[-30:], 21)
    macd  = round(ema(closes[-30:], 12) - ema(closes, 26), 4)
    rsi_v = rsi_calc(closes)
    bb_mid = sma20

    if bb_mid:
        std   = (sum((c - bb_mid)**2 for c in closes[-20:]) / 20) ** 0.5
        bb_u  = bb_mid + 2 * std
        bb_l  = bb_mid - 2 * std
        bb_pct = round((close - bb_l) / (bb_u - bb_l) * 100, 1) if bb_u != bb_l else 50
    else:
        bb_pct = None

    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else None
    mom_5d    = (round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)
                 if len(closes) >= 6 else None)

    return {
        "close":    round(close, 2),
        "rsi":      rsi_v,
        "macd":     macd,
        "sma20":    round(sma20, 2) if sma20 else None,
        "sma50":    round(sma50, 2) if sma50 else None,
        "ema9":     round(ema9,  2),
        "ema21":    round(ema21, 2),
        "bb_pct":   bb_pct,
        "vol_ratio": vol_ratio,
        "mom_5d":   mom_5d,
    }


def _empty_projection(symbol, error_msg=""):
    return {
        "symbol":        symbol,
        "error":         error_msg,
        "proj_high":     None,
        "proj_low":      None,
        "pivot":         None,
        "atr":           None,
        "confidence":    0,
        "bias":          "unknown",
        "signal_summary": f"{symbol}: projection unavailable ({error_msg})",
        "trade_action":  "skip — no projection data",
    }
