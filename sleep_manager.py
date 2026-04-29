"""
sleep_manager.py — NovaTrade AI Sleep/Wake System
══════════════════════════════════════════════════
Controls when AIs sleep and wake. Bot runs autonomously
between sleep cycles — zero API cost while sleeping.
"""

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo  # ← FIX: required for now_et = datetime.now(ZoneInfo(...))
                                #         missing import was crashing check_wake_conditions
                                #         which cascaded into autonomous monitor unpacking None

# ── Shared references (injected by bot) ──────────────────────
log          = print
shared_state = {}
get_cash_thresholds  = None
get_spy_trend        = None


save_state_fn = None   # Injected — saves persistent state to volume

def _set_context(log_fn, shared_state_ref,
                 get_cash_thresholds_fn=None,
                 get_spy_trend_fn=None,
                 save_state_fn_ref=None):
    """Called by bot to inject shared state and functions."""
    global log, shared_state, get_cash_thresholds, get_spy_trend, save_state_fn
    log          = log_fn
    shared_state = shared_state_ref
    if get_cash_thresholds_fn:
        get_cash_thresholds = get_cash_thresholds_fn
    if get_spy_trend_fn:
        get_spy_trend = get_spy_trend_fn
    if save_state_fn_ref:
        save_state_fn = save_state_fn_ref


def ai_sleep(reason="trades executed — waiting for cash threshold"):
    """Put both AIs to sleep. Bot takes over autonomous execution."""
    shared_state["ai_sleeping"]     = True
    shared_state["sleep_reason"]    = reason
    shared_state["last_sleep_time"] = datetime.now().isoformat()
    shared_state["wake_reason"]     = None
    shared_state["stops_fired_today"] = 0
    try:
        if save_state_fn: save_state_fn()
    except Exception: pass

    # Store current exit strategies so bot can execute them while AIs sleep
    shared_state["sleeping_strategies"] = dict(shared_state["position_exits"])

    # Clear old scan results so next wake gets fresh data
    shared_state["trend_scan_results"] = []
    shared_state["trend_alerts"]       = []
    shared_state["deposit_detected"]   = False
    shared_state["stops_fired_today"]  = 0

    log(f"😴 AIs going to SLEEP — {reason}")
    log(f"   Bot running autonomously with stored strategies")
    log(f"   Positions covered: {list(shared_state['sleeping_strategies'].keys()) or 'none'}")
    instrs = shared_state.get("ai_wake_instructions", [])
    if instrs:
        log(f"   🤖 Custom wake triggers ({len(instrs)}):")
        for i in instrs:
            log(f"      • {i.get('type')} {i.get('symbol','')} @ {i.get('threshold')} — {i.get('reason','')[:50]}")
    log(f"   Wake conditions:")
    log(f"     1. Cash crosses active threshold")
    log(f"     2. All positions closed + cash available")
    log(f"     3. 2+ stop-losses fire (market emergency)")
    log(f"     4. 8:30am premarket (always)")
    log(f"     5. SPY drops >2% suddenly (market crash guard)")
    log(f"     6. AI custom instructions ({len(instrs)} active)")

def ai_wake(reason):
    """Wake both AIs — they resume full analysis and decision making."""
    was_sleeping = shared_state["ai_sleeping"]
    shared_state["ai_sleeping"]    = False
    shared_state["wake_reason"]    = reason
    shared_state["last_wake_time"] = datetime.now().isoformat()
    try:
        if save_state_fn: save_state_fn()
    except Exception: pass

    if was_sleeping:
        sleep_time = shared_state.get("last_sleep_time","")
        if sleep_time:
            try:
                slept_mins = (datetime.now() - datetime.fromisoformat(sleep_time)).seconds // 60
                log(f"🌅 AIs WAKING UP — {reason}")
                log(f"   Slept for {slept_mins} minutes")
                log(f"   Bot executed {shared_state['stops_fired_today']} stop/TP autonomously")

                # Show what bot found during sleep
                scan_results = shared_state.get("trend_scan_results", [])
                alerts       = shared_state.get("trend_alerts", [])
                deposits     = [a for a in alerts if a.get("type") == "deposit"]
                high_pri     = [f for f in scan_results if f.get("priority") == "HIGH"]

                if scan_results:
                    log(f"   Trend scan found {len(scan_results)} items "
                        f"({len(high_pri)} high priority)")
                    for f in high_pri[:3]:
                        log(f"   ⭐ [{f['type']}] {f['symbol']}: {f['note'][:60]}")
                if deposits:
                    total_dep = sum(d.get("amount",0) for d in deposits)
                    log(f"   💵 New deposits while sleeping: +${total_dep:.2f}")
            except Exception:
                log(f"🌅 AIs WAKING UP — {reason}")
        else:
            log(f"🌅 AIs WAKING UP — {reason}")

def check_wake_conditions(cash, equity, positions, spy_change=0):
    """Check all wake conditions; returns (should_wake, reason). No AI calls."""
    # Run stale-restriction cleanup first — best-effort, never raises
    _cleanup_stale_restrictions(positions)
    try:
        if not shared_state["ai_sleeping"]:
            return False, None
        thresholds = get_cash_thresholds(equity)
        prev_cash = shared_state.get("last_cash", 0.0)
        active    = thresholds["active"]
        if cash >= active and prev_cash < active and prev_cash > 0:
            return True, f"cash ${cash:.2f} crossed active threshold ${active:.2f}"
        if len(positions) == 0 and cash >= thresholds["sleep"]:
            return True, f"all positions closed — ${cash:.2f} available"
        stops = shared_state["stops_fired_today"]
        if stops >= 2:
            return True, f"EMERGENCY — {stops} stop-losses fired"
        if spy_change <= -2.0:
            return True, f"EMERGENCY — SPY dropped {spy_change:.1f}%"
        instr_wake, instr_reason = check_ai_wake_instructions(positions, cash, equity)
        if instr_wake:
            return True, instr_reason
        return False, None
    except Exception as e:
        log(f"⚠️ check_wake_conditions: {e}")
        # CRITICAL: must always return a tuple — caller does:
        #   should_wake, wake_reason = check_wake_conditions(...)
        # Returning None here causes "cannot unpack non-iterable NoneType"
        # which then crashes the autonomous monitor every cycle.
        return False, None

def _cleanup_stale_restrictions(positions):
    """
    Auto-clear stale position restrictions (e.g., 403'd by Alpaca).
    Called separately from check_wake_conditions so a failure here
    can never break the wake/sleep logic.
    """
    try:
        restricted = shared_state.get("restricted_positions", set())
        if not restricted:
            return
        # Build symbol set defensively — positions can be list of dicts or empty
        try:
            current_syms = {p["symbol"] for p in positions} if positions else set()
        except Exception:
            current_syms = set()
        cleared = {s for s in restricted if s not in current_syms}
        if cleared:
            shared_state["restricted_positions"] -= cleared
            for s in cleared:
                log(f"🔓 {s} restriction cleared — position no longer held")
    except Exception:
        pass  # Cleanup is best-effort — never raise

def check_ai_wake_instructions(positions, cash, equity):
    """Check AI custom wake instructions against current market state."""
    instructions = shared_state.get("ai_wake_instructions", [])
    if not instructions:
        return False, None

    now_et = datetime.now(ZoneInfo("America/New_York"))
    pos_map = {p["symbol"]: p for p in positions}

    for instr in instructions:
        itype     = instr.get("type", "")
        symbol    = instr.get("symbol", "")
        threshold = instr.get("threshold", 0)
        reason    = instr.get("reason", "AI wake instruction triggered")
        priority  = instr.get("priority", "normal")

        try:
            # ── Price conditions ──────────────────────────────
            if itype == "price_above" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["current_price"]) >= threshold:
                    return True, f"🤖 AI instruction: {symbol} above ${threshold} — {reason}"

            elif itype == "price_below" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["current_price"]) <= threshold:
                    return True, f"🤖 AI instruction: {symbol} below ${threshold} — {reason}"

            # ── P&L conditions ────────────────────────────────
            elif itype == "pnl_above" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["unrealized_plpc"]) * 100 >= threshold:
                    return True, f"🤖 AI instruction: {symbol} P&L +{threshold}% reached — {reason}"

            elif itype == "pnl_below" and symbol:
                pos = pos_map.get(symbol)
                if pos and float(pos["unrealized_plpc"]) * 100 <= threshold:
                    return True, f"🤖 AI instruction: {symbol} P&L {threshold}% hit — {reason}"

            # ── Cash condition ────────────────────────────────
            elif itype == "cash_above":
                if cash >= threshold:
                    return True, f"🤖 AI instruction: cash ${cash:.2f} > ${threshold} — {reason}"

            # ── Time condition ────────────────────────────────
            elif itype == "time_after":
                # threshold is "HH:MM" string
                try:
                    h, m = str(threshold).split(":")
                    target = now_et.replace(hour=int(h), minute=int(m),
                                           second=0, microsecond=0)
                    if now_et >= target:
                        # Remove this instruction after triggering (one-shot)
                        shared_state["ai_wake_instructions"].remove(instr)
                        return True, f"🤖 AI instruction: scheduled wake at {threshold} ET — {reason}"
                except Exception:
                    pass

            # ── SPY conditions ────────────────────────────────
            elif itype in ("spy_above", "spy_below"):
                try:
                    spy_trend, spy_price, _, spy_chg = get_spy_trend()
                    if itype == "spy_above" and spy_price >= threshold:
                        return True, f"🤖 AI instruction: SPY ${spy_price:.2f} above ${threshold} — {reason}"
                    elif itype == "spy_below" and spy_price <= threshold:
                        return True, f"🤖 AI instruction: SPY ${spy_price:.2f} below ${threshold} — {reason}"
                except Exception:
                    pass

        except Exception as ie:
            log(f"⚠️ Wake instruction check error ({itype}): {ie}")

    return False, None
