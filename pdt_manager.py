"""
pdt_manager.py — NovaTrade PDT Manager
═══════════════════════════════════════
Pattern Day Trader rule management.
Tracks intraday buys, prevents PDT violations,
runs AI council for hold/exit decisions.
"""

import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── Shared references (injected by bot) ──────────────────────
log          = print
shared_state = {}
RULES        = {}

# Functions injected from other modules
alpaca           = None
ask_claude       = None
ask_grok         = None
parse_json       = None
smart_sell       = None
record_trade     = None
get_bars         = None
compute_indicators = None


def _set_context(log_fn, shared_state_ref, rules,
                 alpaca_fn=None, ask_claude_fn=None,
                 ask_grok_fn=None, parse_json_fn=None,
                 smart_sell_fn=None, record_trade_fn=None,
                 get_bars_fn=None, compute_indicators_fn=None):
    """Called by bot to inject all dependencies."""
    global log, shared_state, RULES
    global alpaca, ask_claude, ask_grok, parse_json
    global smart_sell, record_trade, get_bars, compute_indicators
    log                = log_fn
    shared_state       = shared_state_ref
    RULES              = rules
    if alpaca_fn:           alpaca             = alpaca_fn
    if ask_claude_fn:       ask_claude         = ask_claude_fn
    if ask_grok_fn:         ask_grok           = ask_grok_fn
    if parse_json_fn:       parse_json         = parse_json_fn
    if smart_sell_fn:       smart_sell         = smart_sell_fn
    if record_trade_fn:     record_trade       = record_trade_fn
    if get_bars_fn:         get_bars           = get_bars_fn
    if compute_indicators_fn: compute_indicators = compute_indicators_fn


def record_intraday_buy(symbol: str):
    """Record a buy so we can detect if selling same day = day trade."""
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    shared_state["intraday_buys"][symbol] = today
    log(f"📋 PDT: recorded intraday buy → {symbol} on {today} "
        f"({shared_state['day_trade_count']}/3 day trades used)")

def is_day_trade(symbol: str) -> bool:
    """Check if selling this symbol today would be a day trade."""
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    return shared_state["intraday_buys"].get(symbol) == today

def get_stock_tier(equity: float) -> dict:
    """Get current stock trading tier based on equity."""
    for t in RULES["stock_tiers"]:
        if t["min_equity"] <= equity < t["max_equity"]:
            return t
    return RULES["stock_tiers"][-1]

def reset_intraday_buys_if_new_day():
    """
    Call at start of each trading day to reset PDT daily tracking.
    Resets day_trade_count daily, keeps 5-day rolling window.
    """
    today     = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    last_date = shared_state.get("pdt_last_reset_date")
    if last_date == today:
        return  # Already reset today

    existing = shared_state.get("intraday_buys", {})
    stale = [s for s, d in existing.items() if d != today]
    for s in stale:
        del shared_state["intraday_buys"][s]

    # Reset daily counter, keep rolling window
    shared_state["pdt_last_reset_date"] = today
    shared_state["day_trade_count"]     = 0
    if stale:
        log(f"📋 PDT: new day — cleared {len(stale)} intraday records, counter reset to 0/3")

def check_pdt_safe(symbol: str, equity: float = 55.0) -> tuple:
    """Check if selling symbol is PDT-safe. Returns (safe, reason)."""
    try:
        if equity >= 25000:
            return True, "equity >= $25k — PDT not applicable"
        if not is_day_trade(symbol):
            return True, "not a day trade (bought on a different day)"
        used = shared_state.get("day_trade_count", 0)
        if used >= 3:
            return False, (f"PDT LIMIT REACHED: {used}/3 day trades this week — "
                           f"cannot sell {symbol} today.")
        return True, f"day trade OK — {3 - used} of 3 remaining"
    except Exception as e:
        log(f"⚠️ check_pdt_safe: {e}")
        return True, "pdt_check_error — defaulting safe"

def run_pdt_hold_council(symbol: str, pos: dict,
                          ask_claude_fn, ask_grok_fn) -> dict:
    """Run PDT hold council - both AIs simulate holding vs selling."""
    try:
        entry_price   = float(pos.get("avg_entry_price", 0))
        current_price = float(pos.get("current_price", entry_price))
        qty           = float(pos.get("qty", 0))
        pnl_pct       = round((current_price - entry_price) / entry_price * 100, 2)
        pnl_usd       = round((current_price - entry_price) * qty, 2)
        used          = shared_state.get("day_trade_count", 0)

        log(f"🤝 PDT HOLD COUNCIL: {symbol} — both AIs simulating multi-day outlook...")
        log(f"   Entry=${entry_price} | Now=${current_price} | "
            f"P&L={pnl_pct:+.1f}% (${pnl_usd:+.2f}) | PDT {used}/3")

        # ── Build multi-day projection data ──────────────────
        bars = get_bars(symbol, days=60)
        ind  = compute_indicators(bars)

        # Day 1 projection (today's remaining range)
        proj_d1 = get_projection(symbol, bars, ind=ind,
                                 open_price=current_price)

        # Simulate Day 2 and Day 3 using trend momentum
        # We shift the anchor price by the daily expected move (ATR)
        atr         = proj_d1.get("atr", current_price * 0.02)
        trend_score = proj_d1.get("trend_score", 0.5)
        bias        = proj_d1.get("bias", "neutral")
        rsi         = ind.get("rsi", 50) if ind else 50
        macd        = ind.get("macd", 0) if ind else 0
        mom_5d      = ind.get("mom_5d", 0) if ind else 0

        # Direction multiplier from trend
        direction   = (trend_score - 0.5) * 2   # -1 to +1
        daily_drift = atr * direction * 0.4      # conservative estimate

        # Day-by-day simulation
        simulated = []
        anchor = current_price
        for day in range(1, 5):
            anchor_next  = round(anchor + daily_drift, 2)
            proj_high_d  = round(anchor_next + atr * 0.6, 2)
            proj_low_d   = round(anchor_next - atr * 0.6, 2)
            # Decay: momentum fades after day 2
            daily_drift *= 0.75
            simulated.append({
                "day":        day,
                "label":      f"+{day}d",
                "anchor":     anchor_next,
                "proj_high":  proj_high_d,
                "proj_low":   proj_low_d,
                "net_pct":    round((anchor_next - entry_price) / entry_price * 100, 2),
            })
            anchor = anchor_next

        # Find the plateau — day when price stops rising meaningfully
        plateau_day = 1
        for i, d in enumerate(simulated):
            if i == 0:
                continue
            prev_high = simulated[i-1]["proj_high"]
            gain      = (d["proj_high"] - prev_high) / prev_high * 100
            if gain < 0.3:   # Less than 0.3% expected additional gain
                plateau_day = i
                break
            plateau_day = i + 1

        plateau = simulated[min(plateau_day, len(simulated)-1)]

        # ── Build simulation prompt for both AIs ─────────────
        sim_rows = "\n".join(
            f"  Day +{d['day']}: est=${d['anchor']:.2f} "
            f"range=[${d['proj_low']:.2f}–${d['proj_high']:.2f}] "
            f"vs entry={d['net_pct']:+.1f}%"
            for d in simulated
        )

        prompt = f"""=== PDT HOLD COUNCIL — {symbol} ===
SITUATION: PDT rule blocks selling {symbol} today ({used}/3 day trades used).
We must decide: how long to hold, where to exit, and where to stop.

CURRENT POSITION:
  Entry: ${entry_price} | Now: ${current_price} | P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})
  Qty: {qty} shares | PDT day trades used: {used}/3

TECHNICAL STATE:
  Bias: {bias.upper()} | Trend score: {trend_score:.2f} | ATR: ${atr:.2f}
  RSI: {rsi} | MACD: {macd:.4f} | 5d momentum: {mom_5d:+.2f}%

MULTI-DAY PRICE SIMULATION (bot projection model):
{sim_rows}
  Estimated plateau: Day +{plateau_day} at ~${plateau['proj_high']:.2f}
  (momentum decays ~25% each day — exit before plateau for best price)

TASK — HOLD COUNCIL DECISION:
1. HOW LONG to hold? (1, 2, 3, or 4 days) — based on simulation above
2. TARGET EXIT PRICE — where to place limit sell order
3. STOP PRICE — where to cut loss if wrong (NEVER below entry × 0.96)
4. TRAIL: Should stop follow price up? (yes/no)
5. DAILY REASSESS: Under what conditions should the plan change?
   (e.g. "if price hits $X sell immediately" or "if RSI > 70 exit")

CRITICAL RULES:
- Exit target must be realistic based on simulation — no wishful thinking
- Stop must be BELOW current price (protecting downside)
- If both AIs disagree on days, take the LOWER number (conservative)
- If projection turns BEARISH at any point, exit immediately regardless of plan

Respond in JSON:
{{"hold_days": 2, "exit_target": 385.50, "stop_price": 378.00, "trail_stop": true,
  "confidence": 75, "rationale": "brief", "daily_triggers": {{"sell_if_above": 388.0, "sell_if_below": 379.0}},
  "reassess_note": "Exit early if RSI > 72 or MACD crosses negative"}}"""

        # ── Ask both AIs ──────────────────────────────────────
        log(f"   🔵 Claude running hold simulation...")
        log(f"   🔴 Grok running hold simulation...")

        claude_plan = None
        grok_plan   = None

        try:
            raw = ask_claude_fn(prompt,
                "You are Claude making a multi-day hold decision. "
                "Run the simulation carefully. ONLY valid JSON.")
            claude_plan = parse_json(raw)
            if claude_plan:
                log(f"   🔵 Claude: hold {claude_plan.get('hold_days')}d "
                    f"exit=${claude_plan.get('exit_target')} "
                    f"stop=${claude_plan.get('stop_price')} "
                    f"conf={claude_plan.get('confidence')}%")
        except Exception as e:
            log(f"   ⚠️ Claude hold council failed: {e}")

        try:
            raw = ask_grok_fn(prompt,
                "You are Grok making a multi-day hold decision with X/Twitter sentiment. "
                "Run the simulation carefully. ONLY valid JSON.")
            grok_plan = parse_json(raw)
            if grok_plan:
                log(f"   🔴 Grok: hold {grok_plan.get('hold_days')}d "
                    f"exit=${grok_plan.get('exit_target')} "
                    f"stop=${grok_plan.get('stop_price')} "
                    f"conf={grok_plan.get('confidence')}%")
        except Exception as e:
            log(f"   ⚠️ Grok hold council failed: {e}")

        # ── Collaborate — merge both plans ────────────────────
        if not claude_plan and not grok_plan:
            log(f"   ⚠️ Both AIs failed — using projection-based fallback")
            return _pdt_fallback_plan(symbol, entry_price, current_price,
                                      plateau, atr, simulated)

        if claude_plan and not grok_plan:
            agreed_plan = claude_plan
            log(f"   🔵 Claude solo — using Claude plan")
        elif grok_plan and not claude_plan:
            agreed_plan = grok_plan
            log(f"   🔴 Grok solo — using Grok plan")
        else:
            # Both responded — negotiate
            c_days   = claude_plan.get("hold_days", 2)
            g_days   = grok_plan.get("hold_days", 2)
            c_exit   = claude_plan.get("exit_target", current_price * 1.05)
            g_exit   = grok_plan.get("exit_target", current_price * 1.05)
            c_stop   = claude_plan.get("stop_price", entry_price * 0.96)
            g_stop   = grok_plan.get("stop_price", entry_price * 0.96)
            c_conf   = claude_plan.get("confidence", 60)
            g_conf   = grok_plan.get("confidence", 60)

            # Conservative: take lower hold days, average exit, higher stop
            agreed_days = min(c_days, g_days)
            agreed_exit = round((c_exit + g_exit) / 2, 2)
            agreed_stop = round(max(c_stop, g_stop), 2)   # Higher = tighter
            agreed_conf = round((c_conf + g_conf) / 2)
            agreed_trail= claude_plan.get("trail_stop", False) or \
                          grok_plan.get("trail_stop", False)

            # Merge daily triggers — take the MORE conservative
            c_trig = claude_plan.get("daily_triggers", {})
            g_trig = grok_plan.get("daily_triggers", {})
            triggers = {
                "sell_if_above": min(
                    c_trig.get("sell_if_above", 9999),
                    g_trig.get("sell_if_above", 9999)
                ),
                "sell_if_below": max(
                    c_trig.get("sell_if_below", 0),
                    g_trig.get("sell_if_below", 0)
                ),
            }

            agreed_plan = {
                "hold_days":     agreed_days,
                "exit_target":   agreed_exit,
                "stop_price":    agreed_stop,
                "trail_stop":    agreed_trail,
                "confidence":    agreed_conf,
                "daily_triggers": triggers,
                "reassess_note": claude_plan.get("reassess_note", ""),
                "rationale":     f"Claude:{c_days}d/${c_exit} + Grok:{g_days}d/${g_exit} → agreed",
                "claude_plan":   claude_plan,
                "grok_plan":     grok_plan,
            }

            log(f"   🤝 AGREED: hold {agreed_days}d | "
                f"exit=${agreed_exit} | stop=${agreed_stop} | conf={agreed_conf}%")
            if triggers["sell_if_above"] < 9999:
                log(f"   📈 Trigger: sell immediately if price > ${triggers['sell_if_above']}")
            if triggers["sell_if_below"] > 0:
                log(f"   📉 Trigger: sell immediately if price < ${triggers['sell_if_below']}")

        # ── Store the plan ────────────────────────────────────
        now_et   = datetime.now(ZoneInfo("America/New_York"))
        plan_key = f"pdt_hold_{symbol}"
        agreed_plan.update({
            "symbol":        symbol,
            "entry_price":   entry_price,
            "price_at_plan": current_price,
            "pnl_at_plan":   pnl_pct,
            "plan_date":     now_et.date().isoformat(),
            "plan_time":     now_et.strftime("%H:%M ET"),
            "expires_days":  agreed_plan.get("hold_days", 2),
            "simulation":    simulated,
            "plateau_day":   plateau_day,
            "plateau_price": plateau["proj_high"],
        })
        shared_state[plan_key] = agreed_plan
        log(f"   ✅ Hold plan stored: exit ${agreed_plan['exit_target']} "
            f"in {agreed_plan['hold_days']} day(s) | "
            f"plateau est. ${plateau['proj_high']:.2f} on day +{plateau_day}")

        return agreed_plan

    except Exception as e:
        log(f"   ❌ Hold council error: {e}")
        return {}

def _pdt_fallback_plan(symbol, entry_price, current_price,
                       plateau, atr, simulated) -> dict:
    """Projection-only fallback when both AIs fail."""
    stop  = round(max(entry_price * 0.96, current_price - 1.5 * atr), 2)
    return {
        "hold_days":     min(plateau["day"], 2),
        "exit_target":   plateau["proj_high"],
        "stop_price":    stop,
        "trail_stop":    True,
        "confidence":    55,
        "rationale":     "Projection-based fallback (AIs unavailable)",
        "simulation":    simulated,
        "plateau_day":   plateau["day"],
        "plateau_price": plateau["proj_high"],
    }

def check_pdt_hold_plans():
    """
    Called every 5-min tick. Checks all active hold plans:
    - Has price hit the exit target? → sell now
    - Has price hit stop? → cut loss
    - Has price surged (>5%)? → re-run hold council with new data
    - Is plan expired (hold_days elapsed)? → sell at market
    Called from the autonomous bot loop (no AI needed for basic checks).
    """
    plans_to_check = {k: v for k, v in shared_state.items()
                      if k.startswith("pdt_hold_")}
    if not plans_to_check:
        return

    try:
        positions  = {p["symbol"]: p for p in alpaca("GET", "/v2/positions")}
        account    = alpaca("GET", "/v2/account")
        equity     = float(account.get("equity", 55))
        now_et     = datetime.now(ZoneInfo("America/New_York"))
        today      = now_et.date().isoformat()
    except Exception:
        return

    for plan_key, plan in list(plans_to_check.items()):
        symbol    = plan.get("symbol", "")
        pos       = positions.get(symbol)
        if not pos:
            # Position no longer exists — clean up plan
            del shared_state[plan_key]
            continue

        curr      = float(pos.get("current_price", 0))
        entry     = plan.get("entry_price", 0)
        exit_tgt  = plan.get("exit_target", 0)
        stop_px   = plan.get("stop_price", 0)
        hold_days = plan.get("hold_days", 2)
        plan_date = plan.get("plan_date", today)
        triggers  = plan.get("daily_triggers", {})
        pnl_pct   = round((curr - entry) / entry * 100, 2) if entry else 0

        # Days elapsed since plan was made
        try:
            days_elapsed = (now_et.date() -
                __import__("datetime").date.fromisoformat(plan_date)).days
        except Exception:
            days_elapsed = 0

        action = None
        reason = ""

        # ── Check all exit conditions ─────────────────────────
        if curr >= exit_tgt and exit_tgt > 0:
            action = "sell"
            reason = f"🎯 PDT hold plan: exit target ${exit_tgt} HIT at ${curr:.2f} (+{pnl_pct:.1f}%)"

        elif curr <= stop_px and stop_px > 0:
            action = "sell"
            reason = f"🛑 PDT hold plan: stop ${stop_px} hit at ${curr:.2f} ({pnl_pct:.1f}%)"

        elif triggers.get("sell_if_above", 0) and curr >= triggers["sell_if_above"]:
            action = "sell"
            reason = f"📈 PDT trigger: price ${curr:.2f} above trigger ${triggers['sell_if_above']}"

        elif triggers.get("sell_if_below", 0) and curr <= triggers["sell_if_below"]:
            action = "sell"
            reason = f"📉 PDT trigger: price ${curr:.2f} below trigger ${triggers['sell_if_below']}"

        elif days_elapsed >= hold_days:
            action = "sell"
            reason = f"⏰ PDT hold plan expired: {days_elapsed} days elapsed (planned {hold_days}d)"

        # ── Surge detection: re-run council if price jumped >5% ──
        price_at_plan = plan.get("price_at_plan", entry)
        surge_pct     = (curr - price_at_plan) / price_at_plan * 100 if price_at_plan else 0
        if surge_pct >= 5.0 and action != "sell":
            log(f"📊 PDT SURGE: {symbol} +{surge_pct:.1f}% since plan — "
                f"flagging for AI reassessment next cycle")
            shared_state[plan_key]["needs_reassess"] = True
            shared_state[plan_key]["price_at_plan"]  = curr  # Reset baseline

        # ── Trail stop update ─────────────────────────────────
        if plan.get("trail_stop") and action != "sell" and curr > stop_px:
            atr_val   = shared_state.get("last_projections", {}).get(
                symbol, {}).get("atr", curr * 0.02)
            new_stop  = round(curr - 1.0 * atr_val, 2)
            if new_stop > stop_px:
                shared_state[plan_key]["stop_price"] = new_stop
                log(f"🔼 PDT trail stop: {symbol} ${stop_px} → ${new_stop} "
                    f"(curr=${curr:.2f} +{pnl_pct:.1f}%)")

        if action == "sell":
            log(f"🤖 PDT AUTO-EXIT: {reason}")
            try:
                current_price = float(pos.get("current_price", 0))
                entry_price   = float(pos.get("avg_entry_price", current_price))
                qty           = pos.get("qty")
                pnl_usd = round((current_price - entry_price) * float(qty or 0), 2)
                pnl_pct = round((current_price - entry_price) / entry_price, 4) if entry_price else 0
                if smart_sell(symbol, reason, pos):
                    record_trade("sell", symbol, qty, current_price,
                                 round(current_price * float(qty or 0), 2),
                                 pos.get("owner","bot"),
                                 pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                                 reason=f"PDT auto-exit: {reason}",
                                 entry_price=entry_price)
                del shared_state[plan_key]
                log(f"   ✅ PDT plan closed for {symbol}")
            except Exception as e:
                log(f"   ❌ PDT auto-exit failed: {e}")

def get_pdt_decision(symbol: str, equity: float,
                     current_price: float, entry_price: float,
                     projections: dict) -> dict:
    """Get PDT-safe sell decision for a position."""
    used      = shared_state.get("day_trade_count", 0)
    remaining = max(0, 3 - used)
    pdt_safe, pdt_reason = check_pdt_safe(symbol, equity)

    proj          = projections.get(symbol, {})
    proj_bias     = proj.get("bias", "neutral") if proj else "neutral"
    proj_high     = proj.get("proj_high", 0) if proj else 0
    proj_low      = proj.get("proj_low", 0)  if proj else 0
    proj_conf     = proj.get("confidence", 0) if proj else 0
    atr           = proj.get("atr", 0)       if proj else 0

    pnl_pct       = round((current_price - entry_price) / entry_price * 100, 2)
    is_profitable = current_price > entry_price

    # ── If PDT safe, just sell normally ──────────────────────
    if pdt_safe:
        return {
            "action":            "sell",
            "reason":            pdt_reason,
            "pdt_used":          used,
            "pdt_left":          remaining,
            "proj_bias":         proj_bias,
            "new_stop":          None,
        }

    # ── PDT would be violated — use projection to decide ─────
    # Priority 1: If losing money AND projection is bearish → sell anyway
    # (losing money now + bearish tomorrow = must cut loss)
    if not is_profitable and proj_bias == "bearish" and proj_conf >= 50:
        return {
            "action":   "sell",
            "reason":   (f"OVERRIDE: PDT limit hit but {symbol} is losing "
                         f"({pnl_pct:+.1f}%) AND projection bearish (conf={proj_conf}) "
                         f"— cutting loss now is better than worse loss tomorrow"),
            "pdt_used": used,
            "pdt_left": remaining,
            "proj_bias": proj_bias,
            "new_stop":  None,
            "override":  True,
        }

    # Priority 2: If profitable AND projection bullish → hold overnight
    if is_profitable and proj_bias in ("bullish", "neutral") and proj_high > current_price:
        upside = round((proj_high - current_price) / current_price * 100, 1)
        # Set trail stop to lock in 50% of current profit
        trail_stop = round(entry_price + (current_price - entry_price) * 0.5, 2)
        if atr > 0:
            trail_stop = round(current_price - 1.0 * atr, 2)  # 1×ATR trail

        return {
            "action":            "hold_overnight",
            "reason":            (f"PDT limit — holding overnight: {symbol} +{pnl_pct:.1f}% "
                                  f"proj={proj_bias} high=${proj_high} (+{upside}% upside) "
                                  f"→ trail stop set to ${trail_stop}"),
            "pdt_used":          used,
            "pdt_left":          remaining,
            "proj_bias":         proj_bias,
            "proj_high":         proj_high,
            "proj_low":          proj_low,
            "new_stop":          trail_stop,
            "expected_tomorrow": f"${proj_low}–${proj_high}",
        }

    # Priority 3: Losing but projection neutral/bullish → hold, tighten stop
    if not is_profitable and proj_bias == "bullish":
        tight_stop = round(current_price * 0.98, 2)  # -2% from here
        return {
            "action":   "hold_overnight",
            "reason":   (f"PDT limit — holding: {symbol} {pnl_pct:+.1f}% but "
                         f"projection bullish (conf={proj_conf}) — may recover tomorrow. "
                         f"Tight stop at ${tight_stop}"),
            "pdt_used": used,
            "pdt_left": remaining,
            "proj_bias": proj_bias,
            "new_stop":  tight_stop,
        }

    # Priority 4: Ambiguous — hold with breakeven stop
    breakeven_stop = round(entry_price * 1.002, 2)  # tiny above entry
    return {
        "action":   "hold_overnight",
        "reason":   (f"PDT limit — holding overnight: {symbol} {pnl_pct:+.1f}% "
                     f"proj={proj_bias} — set stop to ${breakeven_stop} to protect entry"),
        "pdt_used": used,
        "pdt_left": remaining,
        "proj_bias": proj_bias,
        "new_stop":  breakeven_stop,
    }

def get_pdt_status(equity: float) -> dict:
    """Return current PDT status for /stats endpoint."""
    used     = shared_state.get("day_trade_count", 0)
    intraday = shared_state.get("intraday_buys", {})
    return {
        "equity":          round(equity, 2),
        "pdt_applies":     equity < 25000,
        "day_trades_used": used,
        "day_trades_left": max(0, 3 - used),
        "intraday_buys":   list(intraday.keys()),
        "warning": (f"⚠️ {used}/3 day trades used — 1 left, be careful!"
                    if used == 2 and equity < 25000
                    else f"🛑 PDT LIMIT REACHED — no more day trades today!"
                    if used >= 3 and equity < 25000
                    else None),
    }
